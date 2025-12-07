/*
  Copyright (c) 2009-2017 Dave Gamble and cJSON contributors
  
  Minimal cJSON implementation for file_agent.
  Full version at: https://github.com/DaveGamble/cJSON
*/

#include <string.h>
#include <stdio.h>
#include <math.h>
#include <stdlib.h>
#include <limits.h>
#include <ctype.h>
#include <float.h>

#include "cJSON.h"

/* ---- Memory management ---- */
static void *(*global_malloc)(size_t sz) = malloc;
static void (*global_free)(void *ptr) = free;

void cJSON_InitHooks(cJSON_Hooks* hooks) {
    if (!hooks) {
        global_malloc = malloc;
        global_free = free;
        return;
    }
    global_malloc = hooks->malloc_fn ? hooks->malloc_fn : malloc;
    global_free = hooks->free_fn ? hooks->free_fn : free;
}

static cJSON *cJSON_New_Item(void) {
    cJSON *node = (cJSON *)global_malloc(sizeof(cJSON));
    if (node) memset(node, 0, sizeof(cJSON));
    return node;
}

void cJSON_Delete(cJSON *item) {
    cJSON *next;
    while (item) {
        next = item->next;
        if (!(item->type & cJSON_IsReference) && item->child) {
            cJSON_Delete(item->child);
        }
        if (!(item->type & cJSON_IsReference) && item->valuestring) {
            global_free(item->valuestring);
        }
        if (!(item->type & cJSON_StringIsConst) && item->string) {
            global_free(item->string);
        }
        global_free(item);
        item = next;
    }
}

void cJSON_free(void *object) {
    global_free(object);
}

/* ---- Error handling ---- */
static const char *global_error = NULL;

const char *cJSON_GetErrorPtr(void) {
    return global_error;
}

/* ---- Type checking ---- */
int cJSON_IsInvalid(const cJSON *item) { return !item || (item->type & 0xFF) == cJSON_Invalid; }
int cJSON_IsFalse(const cJSON *item) { return item && (item->type & 0xFF) == cJSON_False; }
int cJSON_IsTrue(const cJSON *item) { return item && (item->type & 0xFF) == cJSON_True; }
int cJSON_IsBool(const cJSON *item) { return item && ((item->type & 0xFF) == cJSON_True || (item->type & 0xFF) == cJSON_False); }
int cJSON_IsNull(const cJSON *item) { return item && (item->type & 0xFF) == cJSON_NULL; }
int cJSON_IsNumber(const cJSON *item) { return item && (item->type & 0xFF) == cJSON_Number; }
int cJSON_IsString(const cJSON *item) { return item && (item->type & 0xFF) == cJSON_String; }
int cJSON_IsArray(const cJSON *item) { return item && (item->type & 0xFF) == cJSON_Array; }
int cJSON_IsObject(const cJSON *item) { return item && (item->type & 0xFF) == cJSON_Object; }
int cJSON_IsRaw(const cJSON *item) { return item && (item->type & 0xFF) == cJSON_Raw; }

/* ---- Parsing helpers ---- */
typedef struct {
    const unsigned char *content;
    size_t length;
    size_t offset;
} parse_buffer;

#define can_read(buffer, size) ((buffer)->offset + (size) <= (buffer)->length)
#define can_access_at_index(buffer, index) ((buffer)->offset + (index) < (buffer)->length)
#define buffer_at_offset(buffer) ((buffer)->content + (buffer)->offset)

static int parse_value(cJSON *item, parse_buffer *buffer);

static parse_buffer *skip_whitespace(parse_buffer *buffer) {
    if (!buffer || !buffer->content) return NULL;
    while (can_access_at_index(buffer, 0) && 
           (buffer_at_offset(buffer)[0] <= 32)) {
        buffer->offset++;
    }
    if (buffer->offset >= buffer->length) {
        buffer->offset = buffer->length;
    }
    return buffer;
}

static unsigned char *ensure(char **buffer, size_t needed, size_t *length) {
    if (!buffer || !*buffer) return NULL;
    if (needed > *length) {
        size_t new_size = needed * 2;
        char *new_buf = (char *)global_malloc(new_size);
        if (!new_buf) return NULL;
        memcpy(new_buf, *buffer, *length);
        global_free(*buffer);
        *buffer = new_buf;
        *length = new_size;
    }
    return (unsigned char *)*buffer;
}

/* ---- Parse string ---- */
static int parse_string(cJSON *item, parse_buffer *buffer) {
    const unsigned char *input = buffer_at_offset(buffer);
    
    if (*input != '\"') {
        global_error = "Not a string";
        return 0;
    }
    
    input++;
    buffer->offset++;
    
    const unsigned char *start = input;
    size_t len = 0;
    
    /* Find end of string and count length */
    while (*input != '\"') {
        if (*input == '\0') {
            global_error = "Unterminated string";
            return 0;
        }
        if (*input == '\\') {
            input++;
            buffer->offset++;
        }
        input++;
        buffer->offset++;
        len++;
    }
    
    /* Allocate output */
    char *output = (char *)global_malloc(len + 1);
    if (!output) return 0;
    
    /* Copy and unescape */
    input = start;
    size_t out_idx = 0;
    while (*input != '\"') {
        if (*input == '\\') {
            input++;
            switch (*input) {
                case 'b': output[out_idx++] = '\b'; break;
                case 'f': output[out_idx++] = '\f'; break;
                case 'n': output[out_idx++] = '\n'; break;
                case 'r': output[out_idx++] = '\r'; break;
                case 't': output[out_idx++] = '\t'; break;
                case '\"': case '\\': case '/':
                    output[out_idx++] = *input;
                    break;
                case 'u':
                    /* Skip unicode escapes for simplicity */
                    output[out_idx++] = '?';
                    input += 4;
                    break;
                default:
                    output[out_idx++] = *input;
            }
        } else {
            output[out_idx++] = *input;
        }
        input++;
    }
    output[out_idx] = '\0';
    
    buffer->offset++;  /* Skip closing quote */
    
    item->type = cJSON_String;
    item->valuestring = output;
    
    return 1;
}

/* ---- Parse number ---- */
static int parse_number(cJSON *item, parse_buffer *buffer) {
    double number = 0;
    const unsigned char *start = buffer_at_offset(buffer);
    unsigned char *end = NULL;
    
    number = strtod((const char *)start, (char **)&end);
    
    if (start == end) {
        global_error = "Invalid number";
        return 0;
    }
    
    item->valuedouble = number;
    item->valueint = (int)number;
    item->type = cJSON_Number;
    
    buffer->offset += (size_t)(end - start);
    return 1;
}

/* ---- Parse array ---- */
static int parse_array(cJSON *item, parse_buffer *buffer);

/* ---- Parse object ---- */
static int parse_object(cJSON *item, parse_buffer *buffer) {
    cJSON *head = NULL;
    cJSON *current = NULL;
    
    if (*buffer_at_offset(buffer) != '{') {
        global_error = "Not an object";
        return 0;
    }
    
    buffer->offset++;
    skip_whitespace(buffer);
    
    if (can_access_at_index(buffer, 0) && *buffer_at_offset(buffer) == '}') {
        buffer->offset++;
        item->type = cJSON_Object;
        return 1;
    }
    
    /* Parse members */
    do {
        skip_whitespace(buffer);
        
        cJSON *new_item = cJSON_New_Item();
        if (!new_item) goto fail;
        
        if (!head) {
            head = current = new_item;
        } else {
            current->next = new_item;
            new_item->prev = current;
            current = new_item;
        }
        
        /* Parse key */
        if (!parse_string(new_item, buffer)) goto fail;
        new_item->string = new_item->valuestring;
        new_item->valuestring = NULL;
        new_item->type = cJSON_Invalid;
        
        skip_whitespace(buffer);
        
        if (*buffer_at_offset(buffer) != ':') {
            global_error = "Expected ':'";
            goto fail;
        }
        buffer->offset++;
        skip_whitespace(buffer);
        
        /* Parse value */
        if (!parse_value(new_item, buffer)) goto fail;
        
        skip_whitespace(buffer);
        
    } while (can_access_at_index(buffer, 0) && 
             *buffer_at_offset(buffer) == ',' && 
             buffer->offset++);
    
    if (*buffer_at_offset(buffer) != '}') {
        global_error = "Expected '}'";
        goto fail;
    }
    buffer->offset++;
    
    item->type = cJSON_Object;
    item->child = head;
    return 1;
    
fail:
    if (head) cJSON_Delete(head);
    return 0;
}

static int parse_array(cJSON *item, parse_buffer *buffer) {
    cJSON *head = NULL;
    cJSON *current = NULL;
    
    if (*buffer_at_offset(buffer) != '[') {
        global_error = "Not an array";
        return 0;
    }
    
    buffer->offset++;
    skip_whitespace(buffer);
    
    if (can_access_at_index(buffer, 0) && *buffer_at_offset(buffer) == ']') {
        buffer->offset++;
        item->type = cJSON_Array;
        return 1;
    }
    
    do {
        skip_whitespace(buffer);
        
        cJSON *new_item = cJSON_New_Item();
        if (!new_item) goto fail;
        
        if (!head) {
            head = current = new_item;
        } else {
            current->next = new_item;
            new_item->prev = current;
            current = new_item;
        }
        
        if (!parse_value(new_item, buffer)) goto fail;
        
        skip_whitespace(buffer);
        
    } while (can_access_at_index(buffer, 0) && 
             *buffer_at_offset(buffer) == ',' && 
             buffer->offset++);
    
    if (*buffer_at_offset(buffer) != ']') {
        global_error = "Expected ']'";
        goto fail;
    }
    buffer->offset++;
    
    item->type = cJSON_Array;
    item->child = head;
    return 1;
    
fail:
    if (head) cJSON_Delete(head);
    return 0;
}

/* ---- Parse any value ---- */
static int parse_value(cJSON *item, parse_buffer *buffer) {
    if (!buffer || !buffer->content) {
        global_error = "Invalid buffer";
        return 0;
    }
    
    skip_whitespace(buffer);
    
    if (!can_access_at_index(buffer, 0)) {
        global_error = "Unexpected end";
        return 0;
    }
    
    /* Check value type */
    if (can_read(buffer, 4) && strncmp((const char *)buffer_at_offset(buffer), "null", 4) == 0) {
        item->type = cJSON_NULL;
        buffer->offset += 4;
        return 1;
    }
    if (can_read(buffer, 5) && strncmp((const char *)buffer_at_offset(buffer), "false", 5) == 0) {
        item->type = cJSON_False;
        buffer->offset += 5;
        return 1;
    }
    if (can_read(buffer, 4) && strncmp((const char *)buffer_at_offset(buffer), "true", 4) == 0) {
        item->type = cJSON_True;
        buffer->offset += 4;
        return 1;
    }
    if (*buffer_at_offset(buffer) == '\"') {
        return parse_string(item, buffer);
    }
    if (*buffer_at_offset(buffer) == '-' || 
        (*buffer_at_offset(buffer) >= '0' && *buffer_at_offset(buffer) <= '9')) {
        return parse_number(item, buffer);
    }
    if (*buffer_at_offset(buffer) == '[') {
        return parse_array(item, buffer);
    }
    if (*buffer_at_offset(buffer) == '{') {
        return parse_object(item, buffer);
    }
    
    global_error = "Invalid value";
    return 0;
}

/* ---- Public parse functions ---- */
cJSON *cJSON_ParseWithLength(const char *value, size_t length) {
    parse_buffer buffer = {0};
    cJSON *item;
    
    global_error = NULL;
    
    if (!value || length == 0) {
        global_error = "Null input";
        return NULL;
    }
    
    item = cJSON_New_Item();
    if (!item) {
        global_error = "Memory error";
        return NULL;
    }
    
    buffer.content = (const unsigned char *)value;
    buffer.length = length;
    buffer.offset = 0;
    
    if (!parse_value(item, &buffer)) {
        cJSON_Delete(item);
        return NULL;
    }
    
    return item;
}

cJSON *cJSON_Parse(const char *value) {
    return cJSON_ParseWithLength(value, value ? strlen(value) : 0);
}

/* ---- Object/Array access ---- */
int cJSON_GetArraySize(const cJSON *array) {
    cJSON *child;
    int size = 0;
    if (!array) return 0;
    child = array->child;
    while (child) {
        size++;
        child = child->next;
    }
    return size;
}

cJSON *cJSON_GetArrayItem(const cJSON *array, int index) {
    cJSON *child;
    if (!array || index < 0) return NULL;
    child = array->child;
    while (child && index > 0) {
        child = child->next;
        index--;
    }
    return child;
}

cJSON *cJSON_GetObjectItem(const cJSON *object, const char *string) {
    cJSON *child;
    if (!object || !string) return NULL;
    child = object->child;
    while (child) {
        if (child->string && strcasecmp(child->string, string) == 0) {
            return child;
        }
        child = child->next;
    }
    return NULL;
}

cJSON *cJSON_GetObjectItemCaseSensitive(const cJSON *object, const char *string) {
    cJSON *child;
    if (!object || !string) return NULL;
    child = object->child;
    while (child) {
        if (child->string && strcmp(child->string, string) == 0) {
            return child;
        }
        child = child->next;
    }
    return NULL;
}

int cJSON_HasObjectItem(const cJSON *object, const char *string) {
    return cJSON_GetObjectItem(object, string) != NULL;
}

/* ---- Create items ---- */
cJSON *cJSON_CreateNull(void) {
    cJSON *item = cJSON_New_Item();
    if (item) item->type = cJSON_NULL;
    return item;
}

cJSON *cJSON_CreateTrue(void) {
    cJSON *item = cJSON_New_Item();
    if (item) item->type = cJSON_True;
    return item;
}

cJSON *cJSON_CreateFalse(void) {
    cJSON *item = cJSON_New_Item();
    if (item) item->type = cJSON_False;
    return item;
}

cJSON *cJSON_CreateBool(int boolean) {
    cJSON *item = cJSON_New_Item();
    if (item) item->type = boolean ? cJSON_True : cJSON_False;
    return item;
}

cJSON *cJSON_CreateNumber(double num) {
    cJSON *item = cJSON_New_Item();
    if (item) {
        item->type = cJSON_Number;
        item->valuedouble = num;
        item->valueint = (int)num;
    }
    return item;
}

cJSON *cJSON_CreateString(const char *string) {
    cJSON *item = cJSON_New_Item();
    if (item) {
        item->type = cJSON_String;
        item->valuestring = string ? strdup(string) : NULL;
        if (string && !item->valuestring) {
            cJSON_Delete(item);
            return NULL;
        }
    }
    return item;
}

cJSON *cJSON_CreateRaw(const char *raw) {
    cJSON *item = cJSON_New_Item();
    if (item) {
        item->type = cJSON_Raw;
        item->valuestring = raw ? strdup(raw) : NULL;
    }
    return item;
}

cJSON *cJSON_CreateArray(void) {
    cJSON *item = cJSON_New_Item();
    if (item) item->type = cJSON_Array;
    return item;
}

cJSON *cJSON_CreateObject(void) {
    cJSON *item = cJSON_New_Item();
    if (item) item->type = cJSON_Object;
    return item;
}

/* ---- Add to array/object ---- */
static int add_item_to_array(cJSON *array, cJSON *item) {
    if (!array || !item) return 0;
    
    cJSON *child = array->child;
    if (!child) {
        array->child = item;
    } else {
        while (child->next) child = child->next;
        child->next = item;
        item->prev = child;
    }
    return 1;
}

int cJSON_AddItemToArray(cJSON *array, cJSON *item) {
    return add_item_to_array(array, item);
}

static int add_item_to_object(cJSON *object, const char *string, cJSON *item, int constant_key) {
    if (!object || !string || !item) return 0;
    
    if (constant_key) {
        item->string = (char *)string;
        item->type |= cJSON_StringIsConst;
    } else {
        item->string = strdup(string);
        if (!item->string) return 0;
    }
    
    return add_item_to_array(object, item);
}

int cJSON_AddItemToObject(cJSON *object, const char *string, cJSON *item) {
    return add_item_to_object(object, string, item, 0);
}

int cJSON_AddItemToObjectCS(cJSON *object, const char *string, cJSON *item) {
    return add_item_to_object(object, string, item, 1);
}

/* Helper add functions */
cJSON *cJSON_AddNullToObject(cJSON *object, const char *name) {
    cJSON *item = cJSON_CreateNull();
    if (cJSON_AddItemToObject(object, name, item)) return item;
    cJSON_Delete(item);
    return NULL;
}

cJSON *cJSON_AddTrueToObject(cJSON *object, const char *name) {
    cJSON *item = cJSON_CreateTrue();
    if (cJSON_AddItemToObject(object, name, item)) return item;
    cJSON_Delete(item);
    return NULL;
}

cJSON *cJSON_AddFalseToObject(cJSON *object, const char *name) {
    cJSON *item = cJSON_CreateFalse();
    if (cJSON_AddItemToObject(object, name, item)) return item;
    cJSON_Delete(item);
    return NULL;
}

cJSON *cJSON_AddBoolToObject(cJSON *object, const char *name, int boolean) {
    cJSON *item = cJSON_CreateBool(boolean);
    if (cJSON_AddItemToObject(object, name, item)) return item;
    cJSON_Delete(item);
    return NULL;
}

cJSON *cJSON_AddNumberToObject(cJSON *object, const char *name, double number) {
    cJSON *item = cJSON_CreateNumber(number);
    if (cJSON_AddItemToObject(object, name, item)) return item;
    cJSON_Delete(item);
    return NULL;
}

cJSON *cJSON_AddStringToObject(cJSON *object, const char *name, const char *string) {
    cJSON *item = cJSON_CreateString(string);
    if (cJSON_AddItemToObject(object, name, item)) return item;
    cJSON_Delete(item);
    return NULL;
}

cJSON *cJSON_AddRawToObject(cJSON *object, const char *name, const char *raw) {
    cJSON *item = cJSON_CreateRaw(raw);
    if (cJSON_AddItemToObject(object, name, item)) return item;
    cJSON_Delete(item);
    return NULL;
}

cJSON *cJSON_AddObjectToObject(cJSON *object, const char *name) {
    cJSON *item = cJSON_CreateObject();
    if (cJSON_AddItemToObject(object, name, item)) return item;
    cJSON_Delete(item);
    return NULL;
}

cJSON *cJSON_AddArrayToObject(cJSON *object, const char *name) {
    cJSON *item = cJSON_CreateArray();
    if (cJSON_AddItemToObject(object, name, item)) return item;
    cJSON_Delete(item);
    return NULL;
}

/* ---- Print JSON ---- */
typedef struct {
    char *buffer;
    size_t length;
    size_t offset;
    int format;
    int depth;
} printbuffer;

static int print_value(const cJSON *item, printbuffer *buffer);

static int ensure_buffer(printbuffer *p, size_t needed) {
    if (!p || !p->buffer) return 0;
    
    needed += p->offset + 1;
    if (needed <= p->length) return 1;
    
    size_t new_size = needed * 2;
    char *new_buf = (char *)global_malloc(new_size);
    if (!new_buf) return 0;
    
    memcpy(new_buf, p->buffer, p->offset);
    global_free(p->buffer);
    p->buffer = new_buf;
    p->length = new_size;
    
    return 1;
}

static int print_string_ptr(const char *str, printbuffer *buffer) {
    if (!str) str = "";
    
    size_t len = strlen(str);
    if (!ensure_buffer(buffer, len * 6 + 3)) return 0;
    
    char *output = buffer->buffer + buffer->offset;
    *output++ = '\"';
    
    for (const char *p = str; *p; p++) {
        unsigned char c = *p;
        if (c == '\"') { *output++ = '\\'; *output++ = '\"'; }
        else if (c == '\\') { *output++ = '\\'; *output++ = '\\'; }
        else if (c == '\b') { *output++ = '\\'; *output++ = 'b'; }
        else if (c == '\f') { *output++ = '\\'; *output++ = 'f'; }
        else if (c == '\n') { *output++ = '\\'; *output++ = 'n'; }
        else if (c == '\r') { *output++ = '\\'; *output++ = 'r'; }
        else if (c == '\t') { *output++ = '\\'; *output++ = 't'; }
        else if (c < 32) {
            sprintf(output, "\\u%04x", c);
            output += 6;
        }
        else { *output++ = c; }
    }
    
    *output++ = '\"';
    *output = '\0';
    buffer->offset = output - buffer->buffer;
    
    return 1;
}

static int print_number(const cJSON *item, printbuffer *buffer) {
    if (!ensure_buffer(buffer, 64)) return 0;
    
    char *output = buffer->buffer + buffer->offset;
    double d = item->valuedouble;
    
    if (isnan(d) || isinf(d)) {
        strcpy(output, "null");
    } else if (d == (double)item->valueint) {
        sprintf(output, "%d", item->valueint);
    } else {
        sprintf(output, "%g", d);
    }
    
    buffer->offset += strlen(output);
    return 1;
}

static int print_object(const cJSON *item, printbuffer *buffer);
static int print_array(const cJSON *item, printbuffer *buffer);

static int print_value(const cJSON *item, printbuffer *buffer) {
    if (!item) return 0;
    
    switch (item->type & 0xFF) {
        case cJSON_NULL:
            if (!ensure_buffer(buffer, 5)) return 0;
            strcpy(buffer->buffer + buffer->offset, "null");
            buffer->offset += 4;
            return 1;
        case cJSON_False:
            if (!ensure_buffer(buffer, 6)) return 0;
            strcpy(buffer->buffer + buffer->offset, "false");
            buffer->offset += 5;
            return 1;
        case cJSON_True:
            if (!ensure_buffer(buffer, 5)) return 0;
            strcpy(buffer->buffer + buffer->offset, "true");
            buffer->offset += 4;
            return 1;
        case cJSON_Number:
            return print_number(item, buffer);
        case cJSON_Raw:
        case cJSON_String:
            return print_string_ptr(item->valuestring, buffer);
        case cJSON_Array:
            return print_array(item, buffer);
        case cJSON_Object:
            return print_object(item, buffer);
        default:
            return 0;
    }
}

static int print_array(const cJSON *item, printbuffer *buffer) {
    if (!ensure_buffer(buffer, 1)) return 0;
    buffer->buffer[buffer->offset++] = '[';
    
    cJSON *child = item->child;
    while (child) {
        if (!print_value(child, buffer)) return 0;
        
        child = child->next;
        if (child) {
            if (!ensure_buffer(buffer, 1)) return 0;
            buffer->buffer[buffer->offset++] = ',';
        }
    }
    
    if (!ensure_buffer(buffer, 1)) return 0;
    buffer->buffer[buffer->offset++] = ']';
    buffer->buffer[buffer->offset] = '\0';
    
    return 1;
}

static int print_object(const cJSON *item, printbuffer *buffer) {
    if (!ensure_buffer(buffer, 1)) return 0;
    buffer->buffer[buffer->offset++] = '{';
    
    cJSON *child = item->child;
    while (child) {
        if (!print_string_ptr(child->string, buffer)) return 0;
        
        if (!ensure_buffer(buffer, 1)) return 0;
        buffer->buffer[buffer->offset++] = ':';
        
        if (!print_value(child, buffer)) return 0;
        
        child = child->next;
        if (child) {
            if (!ensure_buffer(buffer, 1)) return 0;
            buffer->buffer[buffer->offset++] = ',';
        }
    }
    
    if (!ensure_buffer(buffer, 1)) return 0;
    buffer->buffer[buffer->offset++] = '}';
    buffer->buffer[buffer->offset] = '\0';
    
    return 1;
}

char *cJSON_Print(const cJSON *item) {
    return cJSON_PrintUnformatted(item);  /* Simplified: no formatting */
}

char *cJSON_PrintUnformatted(const cJSON *item) {
    printbuffer buffer = {0};
    buffer.buffer = (char *)global_malloc(256);
    buffer.length = 256;
    buffer.offset = 0;
    buffer.format = 0;
    buffer.depth = 0;
    
    if (!buffer.buffer) return NULL;
    
    if (!print_value(item, &buffer)) {
        global_free(buffer.buffer);
        return NULL;
    }
    
    return buffer.buffer;
}

char *cJSON_PrintBuffered(const cJSON *item, int prebuffer, int fmt) {
    (void)prebuffer;
    (void)fmt;
    return cJSON_PrintUnformatted(item);
}

/* ---- Duplicate and Compare (stubs) ---- */
cJSON *cJSON_Duplicate(const cJSON *item, int recurse) {
    (void)recurse;
    if (!item) return NULL;
    
    char *printed = cJSON_PrintUnformatted(item);
    if (!printed) return NULL;
    
    cJSON *dup = cJSON_Parse(printed);
    global_free(printed);
    
    return dup;
}

int cJSON_Compare(const cJSON *a, const cJSON *b, int case_sensitive) {
    (void)case_sensitive;
    if (!a || !b) return 0;
    if (a->type != b->type) return 0;
    return 1;  /* Simplified comparison */
}

/* ---- Array creation helpers ---- */
cJSON *cJSON_CreateIntArray(const int *numbers, int count) {
    cJSON *array = cJSON_CreateArray();
    if (!array) return NULL;
    for (int i = 0; i < count; i++) {
        cJSON_AddItemToArray(array, cJSON_CreateNumber(numbers[i]));
    }
    return array;
}

cJSON *cJSON_CreateFloatArray(const float *numbers, int count) {
    cJSON *array = cJSON_CreateArray();
    if (!array) return NULL;
    for (int i = 0; i < count; i++) {
        cJSON_AddItemToArray(array, cJSON_CreateNumber(numbers[i]));
    }
    return array;
}

cJSON *cJSON_CreateDoubleArray(const double *numbers, int count) {
    cJSON *array = cJSON_CreateArray();
    if (!array) return NULL;
    for (int i = 0; i < count; i++) {
        cJSON_AddItemToArray(array, cJSON_CreateNumber(numbers[i]));
    }
    return array;
}

cJSON *cJSON_CreateStringArray(const char *const *strings, int count) {
    cJSON *array = cJSON_CreateArray();
    if (!array) return NULL;
    for (int i = 0; i < count; i++) {
        cJSON_AddItemToArray(array, cJSON_CreateString(strings[i]));
    }
    return array;
}

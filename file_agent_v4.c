/*
 * file_agent_v4.c
 * 
 * File agent with:
 *   - Ollama JSON mode (forced structured output)
 *   - Server-side content handling (no base64 from model)
 *   - Robust HTML repair
 *
 * Compile: gcc file_agent_v4.c cJSON.c -o file_agent -lcurl
 */

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdbool.h>
#include <errno.h>
#include <time.h>
#include <sys/stat.h>
#include <dirent.h>
#include <curl/curl.h>
#include <stdarg.h>
#include "cJSON.h"

/* ============================================================
   CONFIGURATION
   ============================================================ */

#define ALLOWED_DIR     "./sandbox"
#define MODEL_NAME      "qwen2.5-coder:7b"
#define OLLAMA_URL      "http://localhost:11434/api/chat"
#define LOG_FILE        "./file_agent.log"
#define MAX_CONTENT     131072
#define MAX_PATH_LEN    1024
#define MAX_HISTORY     20

#define CONFIRM_WRITE   1
#define CONFIRM_DELETE  1

/* ============================================================
   CONVERSATION HISTORY
   ============================================================ */

typedef struct {
    char role[16];
    char *content;
} Message;

typedef struct {
    Message messages[MAX_HISTORY];
    int count;
} Conversation;

static Conversation g_conversation = {0};

static void conversation_clear(void) {
    for (int i = 0; i < g_conversation.count; i++) {
        free(g_conversation.messages[i].content);
        g_conversation.messages[i].content = NULL;
    }
    g_conversation.count = 0;
}

static void conversation_add(const char *role, const char *content) {
    if (g_conversation.count >= MAX_HISTORY) {
        free(g_conversation.messages[0].content);
        memmove(&g_conversation.messages[0], &g_conversation.messages[1], 
                sizeof(Message) * (MAX_HISTORY - 1));
        g_conversation.count--;
    }
    
    int idx = g_conversation.count++;
    strncpy(g_conversation.messages[idx].role, role, 15);
    g_conversation.messages[idx].content = strdup(content);
}

/* ============================================================
   LOGGING
   ============================================================ */

static FILE *g_log_file = NULL;

static void get_timestamp(char *buf, size_t size) {
    time_t now = time(NULL);
    strftime(buf, size, "%Y-%m-%d %H:%M:%S", localtime(&now));
}

static void log_init(void) {
    g_log_file = fopen(LOG_FILE, "a");
    if (g_log_file) {
        char ts[64]; get_timestamp(ts, sizeof(ts));
        fprintf(g_log_file, "\n=== Session %s ===\n", ts);
        fflush(g_log_file);
    }
}

static void log_close(void) {
    if (g_log_file) { fclose(g_log_file); g_log_file = NULL; }
}

static void log_write(const char *level, const char *fmt, ...) {
    char ts[64]; get_timestamp(ts, sizeof(ts));
    va_list args;
    
    /* Print warnings/errors to stderr */
    if (strcmp(level, "ERROR") == 0 || strcmp(level, "WARN") == 0) {
        va_start(args, fmt);
        fprintf(stderr, "[%s] ", level);
        vfprintf(stderr, fmt, args);
        fprintf(stderr, "\n");
        va_end(args);
    }
    
    if (g_log_file) {
        va_start(args, fmt);
        fprintf(g_log_file, "[%s] [%s] ", ts, level);
        vfprintf(g_log_file, fmt, args);
        fprintf(g_log_file, "\n");
        fflush(g_log_file);
        va_end(args);
    }
}

static void log_audit(const char *action, const char *path, const char *result) {
    if (!g_log_file) return;
    char ts[64]; get_timestamp(ts, sizeof(ts));
    fprintf(g_log_file, "[%s] [AUDIT] %s %s -> %s\n", ts, action, path, result);
    fflush(g_log_file);
}

/* ============================================================
   CURL
   ============================================================ */

typedef struct { char *data; size_t size; } Buffer;

static size_t curl_write(void *p, size_t size, size_t n, void *userp) {
    size_t realsize = size * n;
    Buffer *buf = userp;
    char *ptr = realloc(buf->data, buf->size + realsize + 1);
    if (!ptr) return 0;
    buf->data = ptr;
    memcpy(buf->data + buf->size, p, realsize);
    buf->size += realsize;
    buf->data[buf->size] = 0;
    return realsize;
}

/* ============================================================
   HTML REPAIR - Convert ? back to < and >
   ============================================================ */

static char *repair_html(const char *input) {
    if (!input) return NULL;
    
    size_t len = strlen(input);
    char *output = malloc(len + 1);
    if (!output) return NULL;
    
    size_t j = 0;
    for (size_t i = 0; i < len; i++) {
        if (input[i] == '?') {
            /* Look at context to decide if < or > */
            
            /* Check for opening tag patterns: ?tagname, ?!, ?/ */
            if (i + 1 < len) {
                char next = input[i + 1];
                if (next == '!' || next == '/' || 
                    (next >= 'a' && next <= 'z') ||
                    (next >= 'A' && next <= 'Z')) {
                    output[j++] = '<';
                    continue;
                }
            }
            
            /* Check for closing: after tagname, after ", after ', after / */
            if (i > 0) {
                char prev = input[i - 1];
                if ((prev >= 'a' && prev <= 'z') ||
                    (prev >= 'A' && prev <= 'Z') ||
                    (prev >= '0' && prev <= '9') ||
                    prev == '"' || prev == '\'' || 
                    prev == '/' || prev == '-') {
                    output[j++] = '>';
                    continue;
                }
            }
            
            /* Default: keep as ? */
            output[j++] = '?';
        } else {
            output[j++] = input[i];
        }
    }
    output[j] = '\0';
    return output;
}

/* ============================================================
   PATH SAFETY
   ============================================================ */

static bool safe_path(const char *rel, char *out, size_t out_size) {
    if (!rel || !rel[0] || rel[0] == '/' || strstr(rel, "..")) {
        return false;
    }
    snprintf(out, out_size, "%s/%s", ALLOWED_DIR, rel);
    return true;
}

static void ensure_dirs(const char *path) {
    char tmp[MAX_PATH_LEN];
    strncpy(tmp, path, sizeof(tmp) - 1);
    for (char *p = tmp + strlen(ALLOWED_DIR) + 1; *p; p++) {
        if (*p == '/') { *p = 0; mkdir(tmp, 0755); *p = '/'; }
    }
}

/* ============================================================
   CONFIRMATION
   ============================================================ */

static bool confirm(const char *action, const char *path, size_t bytes) {
    printf("\n┌─ CONFIRM %s ─────────────────────────────────────────┐\n", action);
    printf("│  Path: %-50s │\n", path);
    printf("│  Size: %-50zu │\n", bytes);
    printf("└────────────────────────────────────────────────────────────┘\n");
    printf("Proceed? [y/N]: ");
    fflush(stdout);
    
    char resp[16];
    if (!fgets(resp, sizeof(resp), stdin)) return false;
    return (resp[0] == 'y' || resp[0] == 'Y');
}

/* ============================================================
   FILE OPERATIONS
   ============================================================ */

static char *file_read(const char *rel_path, long *size_out) {
    char full[MAX_PATH_LEN];
    if (!safe_path(rel_path, full, sizeof(full))) return NULL;
    
    FILE *f = fopen(full, "r");
    if (!f) return NULL;
    
    fseek(f, 0, SEEK_END);
    long size = ftell(f);
    fseek(f, 0, SEEK_SET);
    
    char *content = malloc(size + 1);
    if (content) {
        fread(content, 1, size, f);
        content[size] = '\0';
    }
    fclose(f);
    
    if (size_out) *size_out = size;
    return content;
}

static bool file_write(const char *rel_path, const char *content, bool append) {
    char full[MAX_PATH_LEN];
    if (!safe_path(rel_path, full, sizeof(full))) return false;
    
    ensure_dirs(full);
    FILE *f = fopen(full, append ? "a" : "w");
    if (!f) return false;
    
    size_t len = strlen(content);
    fwrite(content, 1, len, f);
    fclose(f);
    return true;
}

static bool file_delete(const char *rel_path) {
    char full[MAX_PATH_LEN];
    if (!safe_path(rel_path, full, sizeof(full))) return false;
    return remove(full) == 0;
}

static char *file_list(const char *rel_path) {
    char full[MAX_PATH_LEN];
    
    if (!rel_path || !rel_path[0] || strcmp(rel_path, ".") == 0) {
        strncpy(full, ALLOWED_DIR, sizeof(full));
    } else if (!safe_path(rel_path, full, sizeof(full))) {
        return NULL;
    }
    
    DIR *dir = opendir(full);
    if (!dir) return NULL;
    
    char *result = malloc(4096);
    if (!result) { closedir(dir); return NULL; }
    result[0] = '\0';
    size_t len = 0;
    
    struct dirent *e;
    while ((e = readdir(dir))) {
        if (e->d_name[0] != '.') {
            len += snprintf(result + len, 4096 - len, "%s%s\n", 
                           e->d_name, e->d_type == DT_DIR ? "/" : "");
        }
    }
    closedir(dir);
    return result;
}

/* ============================================================
   OLLAMA API - Using JSON mode
   ============================================================ */

static const char *SYSTEM_PROMPT = 
"You are a file assistant. Respond with JSON only.\n"
"\n"
"Format: {\"action\": \"ACTION\", \"path\": \"PATH\", \"content\": \"CONTENT\"}\n"
"\n"
"Actions:\n"
"- list: List files in directory (use path=\".\" for root)\n"
"- read: Read file contents (I will show you the contents)\n"
"- write: Create or overwrite file\n"
"- append: Add content to end of file\n"
"- delete: Delete a file\n"
"\n"
"For write/append, put the COMPLETE file content in the \"content\" field.\n"
"For read/list/delete, set content to empty string.\n"
"\n"
"IMPORTANT: Return ONLY the JSON object. No explanations.";

static bool call_ollama(char *response, size_t response_size) {
    CURL *curl = curl_easy_init();
    if (!curl) return false;
    
    Buffer buf = {0};
    
    /* Build request with JSON mode */
    cJSON *req = cJSON_CreateObject();
    cJSON_AddStringToObject(req, "model", MODEL_NAME);
    cJSON_AddBoolToObject(req, "stream", false);
    cJSON_AddStringToObject(req, "format", "json");  /* Force JSON output */
    
    cJSON *msgs = cJSON_CreateArray();
    
    /* System message */
    cJSON *sys = cJSON_CreateObject();
    cJSON_AddStringToObject(sys, "role", "system");
    cJSON_AddStringToObject(sys, "content", SYSTEM_PROMPT);
    cJSON_AddItemToArray(msgs, sys);
    
    /* Conversation history */
    for (int i = 0; i < g_conversation.count; i++) {
        cJSON *m = cJSON_CreateObject();
        cJSON_AddStringToObject(m, "role", g_conversation.messages[i].role);
        cJSON_AddStringToObject(m, "content", g_conversation.messages[i].content);
        cJSON_AddItemToArray(msgs, m);
    }
    
    cJSON_AddItemToObject(req, "messages", msgs);
    
    char *post = cJSON_PrintUnformatted(req);
    cJSON_Delete(req);
    
    curl_easy_setopt(curl, CURLOPT_URL, OLLAMA_URL);
    curl_easy_setopt(curl, CURLOPT_POST, 1L);
    curl_easy_setopt(curl, CURLOPT_POSTFIELDS, post);
    curl_easy_setopt(curl, CURLOPT_WRITEFUNCTION, curl_write);
    curl_easy_setopt(curl, CURLOPT_WRITEDATA, &buf);
    curl_easy_setopt(curl, CURLOPT_TIMEOUT, 180L);
    
    struct curl_slist *h = curl_slist_append(NULL, "Content-Type: application/json");
    curl_easy_setopt(curl, CURLOPT_HTTPHEADER, h);
    
    CURLcode res = curl_easy_perform(curl);
    
    free(post);
    curl_slist_free_all(h);
    curl_easy_cleanup(curl);
    
    if (res != CURLE_OK || !buf.data) {
        free(buf.data);
        return false;
    }
    
    /* Parse Ollama response */
    cJSON *resp = cJSON_Parse(buf.data);
    free(buf.data);
    
    if (!resp) return false;
    
    cJSON *msg = cJSON_GetObjectItem(resp, "message");
    cJSON *content = msg ? cJSON_GetObjectItem(msg, "content") : NULL;
    
    if (!cJSON_IsString(content)) {
        cJSON_Delete(resp);
        return false;
    }
    
    strncpy(response, content->valuestring, response_size - 1);
    cJSON_Delete(resp);
    return true;
}

/* ============================================================
   COMMAND PARSING & EXECUTION
   ============================================================ */

typedef struct {
    char action[32];
    char path[MAX_PATH_LEN];
    char *content;
    bool valid;
} Command;

static Command parse_command(const char *json_str) {
    Command cmd = {0};
    
    cJSON *json = cJSON_Parse(json_str);
    if (!json) {
        log_write("WARN", "JSON parse failed: %s", json_str);
        return cmd;
    }
    
    cJSON *action = cJSON_GetObjectItem(json, "action");
    cJSON *path = cJSON_GetObjectItem(json, "path");
    cJSON *content = cJSON_GetObjectItem(json, "content");
    
    if (!cJSON_IsString(action)) {
        cJSON_Delete(json);
        return cmd;
    }
    
    strncpy(cmd.action, action->valuestring, sizeof(cmd.action) - 1);
    
    if (cJSON_IsString(path)) {
        strncpy(cmd.path, path->valuestring, sizeof(cmd.path) - 1);
    }
    
    if (cJSON_IsString(content) && content->valuestring[0]) {
        /* Repair any corrupted HTML in content */
        cmd.content = repair_html(content->valuestring);
    } else {
        cmd.content = strdup("");
    }
    
    cJSON_Delete(json);
    cmd.valid = true;
    return cmd;
}

static void run_command(Command *cmd, const char *user_input) {
    char result[256] = "Unknown action";
    
    if (strcmp(cmd->action, "list") == 0) {
        char *listing = file_list(cmd->path);
        if (listing) {
            printf("\n📁 Contents of %s:\n", cmd->path[0] ? cmd->path : ".");
            printf("────────────────────────────────\n");
            printf("%s", listing);
            printf("────────────────────────────────\n");
            
            /* Add to context */
            char ctx[4096];
            snprintf(ctx, sizeof(ctx), "Directory listing:\n%s", listing);
            conversation_add("assistant", ctx);
            
            snprintf(result, sizeof(result), "Listed files");
            free(listing);
        } else {
            snprintf(result, sizeof(result), "Failed to list");
        }
    }
    else if (strcmp(cmd->action, "read") == 0) {
        long size;
        char *content = file_read(cmd->path, &size);
        if (content) {
            printf("\n📄 %s (%ld bytes):\n", cmd->path, size);
            printf("────────────────────────────────\n");
            printf("%s\n", content);
            printf("────────────────────────────────\n");
            
            /* Add file contents to context for editing */
            char *ctx = malloc(size + 512);
            if (ctx) {
                snprintf(ctx, size + 512,
                    "Contents of %s:\n```\n%s\n```\n"
                    "You can now modify this. Use 'write' with complete new content.",
                    cmd->path, content);
                conversation_add("assistant", ctx);
                free(ctx);
            }
            
            printf("\n✓ File loaded into context for editing\n");
            snprintf(result, sizeof(result), "Read %ld bytes", size);
            free(content);
        } else {
            printf("❌ Could not read %s\n", cmd->path);
            snprintf(result, sizeof(result), "Read failed");
        }
    }
    else if (strcmp(cmd->action, "write") == 0) {
        size_t len = strlen(cmd->content);
        if (len == 0) {
            printf("❌ No content provided for write\n");
            snprintf(result, sizeof(result), "No content");
        } else if (!CONFIRM_WRITE || confirm("WRITE", cmd->path, len)) {
            if (file_write(cmd->path, cmd->content, false)) {
                printf("✓ Wrote %zu bytes to %s\n", len, cmd->path);
                snprintf(result, sizeof(result), "Wrote %zu bytes", len);
            } else {
                printf("❌ Write failed\n");
                snprintf(result, sizeof(result), "Write failed");
            }
        } else {
            snprintf(result, sizeof(result), "Cancelled");
        }
    }
    else if (strcmp(cmd->action, "append") == 0) {
        size_t len = strlen(cmd->content);
        if (file_write(cmd->path, cmd->content, true)) {
            printf("✓ Appended %zu bytes to %s\n", len, cmd->path);
            snprintf(result, sizeof(result), "Appended %zu bytes", len);
        } else {
            printf("❌ Append failed\n");
            snprintf(result, sizeof(result), "Append failed");
        }
    }
    else if (strcmp(cmd->action, "delete") == 0) {
        if (!CONFIRM_DELETE || confirm("DELETE", cmd->path, 0)) {
            if (file_delete(cmd->path)) {
                printf("✓ Deleted %s\n", cmd->path);
                snprintf(result, sizeof(result), "Deleted");
            } else {
                printf("❌ Delete failed\n");
                snprintf(result, sizeof(result), "Delete failed");
            }
        } else {
            snprintf(result, sizeof(result), "Cancelled");
        }
    }
    else {
        printf("❓ Unknown action: %s\n", cmd->action);
    }
    
    log_audit(cmd->action, cmd->path, result);
}

/* ============================================================
   MAIN
   ============================================================ */

static void print_banner(void) {
    printf("\n");
    printf("╔═══════════════════════════════════════════════════════════════╗\n");
    printf("║           FILE AGENT v4 (JSON Mode)                           ║\n");
    printf("╠═══════════════════════════════════════════════════════════════╣\n");
    printf("║  Model:   %-50s   ║\n", MODEL_NAME);
    
    char abs[MAX_PATH_LEN];
    printf("║  Sandbox: %-50s   ║\n", 
           realpath(ALLOWED_DIR, abs) ? abs : ALLOWED_DIR);
    printf("║  Log:     %-50s   ║\n", LOG_FILE);
    printf("╠═══════════════════════════════════════════════════════════════╣\n");
    printf("║  quit, log, context, clear, help                              ║\n");
    printf("╚═══════════════════════════════════════════════════════════════╝\n\n");
}

static void show_help(void) {
    printf("\n");
    printf("Usage Examples:\n");
    printf("  list files           - Show sandbox contents\n");
    printf("  read myfile.txt      - Load file into context\n");
    printf("  create hello.html    - Create new file\n");
    printf("  edit myfile.txt      - Read then modify\n");
    printf("  delete old.txt       - Remove file\n");
    printf("\n");
    printf("For editing: First READ the file, then describe changes.\n");
    printf("The model will see the file contents and generate new version.\n");
    printf("\n");
}

int main(void) {
    mkdir(ALLOWED_DIR, 0755);
    log_init();
    curl_global_init(CURL_GLOBAL_DEFAULT);
    
    print_banner();
    log_write("INFO", "Started with model %s", MODEL_NAME);
    
    char input[2048];
    char response[MAX_CONTENT];
    
    while (1) {
        printf("You: ");
        fflush(stdout);
        
        if (!fgets(input, sizeof(input), stdin)) break;
        
        /* Strip newline */
        size_t len = strlen(input);
        if (len && input[len-1] == '\n') input[--len] = '\0';
        if (!len) continue;
        
        /* Built-in commands */
        if (strcmp(input, "quit") == 0 || strcmp(input, "exit") == 0) break;
        if (strcmp(input, "help") == 0) { show_help(); continue; }
        if (strcmp(input, "clear") == 0) { 
            conversation_clear(); 
            printf("✓ Context cleared\n\n"); 
            continue; 
        }
        if (strcmp(input, "context") == 0) {
            printf("\n=== Context (%d messages) ===\n", g_conversation.count);
            for (int i = 0; i < g_conversation.count; i++) {
                printf("[%s] %.60s%s\n", 
                       g_conversation.messages[i].role,
                       g_conversation.messages[i].content,
                       strlen(g_conversation.messages[i].content) > 60 ? "..." : "");
            }
            printf("=============================\n\n");
            continue;
        }
        if (strcmp(input, "log") == 0) {
            FILE *f = fopen(LOG_FILE, "r");
            if (f) {
                char line[256];
                printf("\n=== Log ===\n");
                while (fgets(line, sizeof(line), f)) printf("%s", line);
                printf("===========\n\n");
                fclose(f);
            }
            continue;
        }
        
        /* Add user message to conversation */
        conversation_add("user", input);
        log_write("INFO", "User: %s", input);
        
        /* Call Ollama */
        printf("🤔 Thinking...\n");
        
        if (!call_ollama(response, sizeof(response))) {
            printf("❌ Failed to get response from model\n\n");
            continue;
        }
        
        log_write("INFO", "Model: %s", response);
        printf("Model: %s\n", response);
        
        /* Parse and execute */
        Command cmd = parse_command(response);
        
        if (!cmd.valid) {
            printf("❌ Could not parse response. Try rephrasing.\n\n");
            continue;
        }
        
        printf("→ Action: %s, Path: %s, Content: %zu bytes\n", 
               cmd.action, cmd.path, cmd.content ? strlen(cmd.content) : 0);
        
        run_command(&cmd, input);
        
        if (cmd.content) free(cmd.content);
        printf("\n");
    }
    
    conversation_clear();
    curl_global_cleanup();
    log_close();
    printf("Goodbye!\n");
    return 0;
}

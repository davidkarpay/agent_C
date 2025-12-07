/*
 * file_agent_v2.c
 * 
 * Enhanced file agent with:
 *   - Conversation memory (model sees read results)
 *   - Edit action (read-modify-write)
 *   - Proper HTML/special character handling
 *   - Multi-turn context
 *
 * Compile: gcc file_agent_v2.c cJSON.c -o file_agent -lcurl
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
#define OLLAMA_URL      "http://localhost:11434/api/chat"  /* Using chat endpoint for context */
#define LOG_FILE        "./file_agent.log"
#define MAX_CONTENT     65536
#define MAX_PATH_LEN    1024
#define MAX_HISTORY     20  /* Keep last N messages for context */

#define CONFIRM_WRITE   1
#define CONFIRM_DELETE  1
#define CONFIRM_APPEND  0

/* ============================================================
   CONVERSATION HISTORY
   ============================================================ */

typedef struct {
    char role[16];      /* "user", "assistant", or "system" */
    char *content;      /* Message content (heap allocated) */
} Message;

typedef struct {
    Message messages[MAX_HISTORY];
    int count;
} Conversation;

static Conversation g_conversation = {0};

static void conversation_clear(void) {
    for (int i = 0; i < g_conversation.count; i++) {
        if (g_conversation.messages[i].content) {
            free(g_conversation.messages[i].content);
            g_conversation.messages[i].content = NULL;
        }
    }
    g_conversation.count = 0;
}

static void conversation_add(const char *role, const char *content) {
    /* If at capacity, remove oldest non-system message */
    if (g_conversation.count >= MAX_HISTORY) {
        /* Find first non-system message to remove */
        int remove_idx = 0;
        for (int i = 0; i < g_conversation.count; i++) {
            if (strcmp(g_conversation.messages[i].role, "system") != 0) {
                remove_idx = i;
                break;
            }
        }
        
        free(g_conversation.messages[remove_idx].content);
        
        /* Shift remaining messages */
        for (int i = remove_idx; i < g_conversation.count - 1; i++) {
            g_conversation.messages[i] = g_conversation.messages[i + 1];
        }
        g_conversation.count--;
    }
    
    /* Add new message */
    int idx = g_conversation.count;
    strncpy(g_conversation.messages[idx].role, role, sizeof(g_conversation.messages[idx].role) - 1);
    g_conversation.messages[idx].content = strdup(content);
    g_conversation.count++;
}

/* ============================================================
   LOGGING SYSTEM
   ============================================================ */

typedef enum {
    LOG_INFO,
    LOG_WARN,
    LOG_ERROR,
    LOG_AUDIT
} LogLevel;

static FILE *g_log_file = NULL;

static const char *log_level_str(LogLevel level) {
    switch (level) {
        case LOG_INFO:  return "INFO";
        case LOG_WARN:  return "WARN";
        case LOG_ERROR: return "ERROR";
        case LOG_AUDIT: return "AUDIT";
        default:        return "UNKNOWN";
    }
}

static void get_timestamp(char *buf, size_t size) {
    time_t now = time(NULL);
    struct tm *tm_info = localtime(&now);
    strftime(buf, size, "%Y-%m-%d %H:%M:%S", tm_info);
}

static void log_init(void) {
    g_log_file = fopen(LOG_FILE, "a");
    if (!g_log_file) {
        fprintf(stderr, "Warning: Could not open log file %s: %s\n", 
                LOG_FILE, strerror(errno));
        return;
    }
    
    char timestamp[64];
    get_timestamp(timestamp, sizeof(timestamp));
    fprintf(g_log_file, "\n========================================\n");
    fprintf(g_log_file, "[%s] [INFO] File Agent v2 Started\n", timestamp);
    fprintf(g_log_file, "========================================\n");
    fflush(g_log_file);
}

static void log_close(void) {
    if (g_log_file) {
        char timestamp[64];
        get_timestamp(timestamp, sizeof(timestamp));
        fprintf(g_log_file, "[%s] [INFO] File Agent Shutdown\n", timestamp);
        fprintf(g_log_file, "========================================\n\n");
        fclose(g_log_file);
        g_log_file = NULL;
    }
}

static void log_write(LogLevel level, const char *format, ...) {
    char timestamp[64];
    get_timestamp(timestamp, sizeof(timestamp));
    
    if (level == LOG_ERROR || level == LOG_WARN) {
        va_list args;
        va_start(args, format);
        fprintf(stderr, "[%s] ", log_level_str(level));
        vfprintf(stderr, format, args);
        fprintf(stderr, "\n");
        va_end(args);
    }
    
    if (g_log_file) {
        va_list args;
        va_start(args, format);
        fprintf(g_log_file, "[%s] [%s] ", timestamp, log_level_str(level));
        vfprintf(g_log_file, format, args);
        fprintf(g_log_file, "\n");
        fflush(g_log_file);
        va_end(args);
    }
}

static void log_audit(const char *user_input, const char *model_response,
                      const char *action, const char *path, 
                      const char *result, bool confirmed) {
    if (!g_log_file) return;
    
    char timestamp[64];
    get_timestamp(timestamp, sizeof(timestamp));
    
    fprintf(g_log_file, "\n--- AUDIT ENTRY ---\n");
    fprintf(g_log_file, "Timestamp: %s\n", timestamp);
    fprintf(g_log_file, "User Input: %s\n", user_input);
    fprintf(g_log_file, "Model Response: %.200s%s\n", model_response, 
            strlen(model_response) > 200 ? "..." : "");
    fprintf(g_log_file, "Action: %s\n", action);
    fprintf(g_log_file, "Path: %s\n", path);
    fprintf(g_log_file, "Confirmed: %s\n", confirmed ? "YES" : "NO/N/A");
    fprintf(g_log_file, "Result: %s\n", result);
    fprintf(g_log_file, "-------------------\n");
    fflush(g_log_file);
}

/* ============================================================
   CURL RESPONSE BUFFER
   ============================================================ */

typedef struct {
    char *data;
    size_t size;
} ResponseBuffer;

static size_t write_callback(void *contents, size_t size, size_t nmemb, void *userp) {
    size_t realsize = size * nmemb;
    ResponseBuffer *buf = (ResponseBuffer *)userp;

    char *ptr = realloc(buf->data, buf->size + realsize + 1);
    if (!ptr) {
        log_write(LOG_ERROR, "Out of memory in curl callback");
        return 0;
    }

    buf->data = ptr;
    memcpy(&buf->data[buf->size], contents, realsize);
    buf->size += realsize;
    buf->data[buf->size] = '\0';

    return realsize;
}

/* ============================================================
   PATH SAFETY
   ============================================================ */

static bool safe_path(const char *relative, char *out, size_t out_size) {
    if (!relative || relative[0] == '\0') {
        log_write(LOG_WARN, "Security: Empty path rejected");
        return false;
    }
    
    if (relative[0] == '/') {
        log_write(LOG_WARN, "Security: Absolute path rejected: %s", relative);
        return false;
    }
    
    if (strstr(relative, "..")) {
        log_write(LOG_WARN, "Security: Path traversal blocked: %s", relative);
        return false;
    }
    
    char full[MAX_PATH_LEN];
    int written = snprintf(full, sizeof(full), "%s/%s", ALLOWED_DIR, relative);
    if (written >= (int)sizeof(full)) {
        log_write(LOG_WARN, "Security: Path too long: %s", relative);
        return false;
    }
    
    char sandbox_real[MAX_PATH_LEN];
    char *resolved_sandbox = realpath(ALLOWED_DIR, sandbox_real);
    
    if (resolved_sandbox) {
        char full_real[MAX_PATH_LEN];
        char *resolved_full = realpath(full, full_real);
        
        if (resolved_full) {
            if (strncmp(full_real, sandbox_real, strlen(sandbox_real)) != 0) {
                log_write(LOG_WARN, "Security: Resolved path escapes sandbox: %s -> %s", 
                          relative, full_real);
                return false;
            }
        }
    }
    
    strncpy(out, full, out_size - 1);
    out[out_size - 1] = '\0';
    
    return true;
}

static void ensure_parent_dirs(const char *path) {
    char tmp[MAX_PATH_LEN];
    strncpy(tmp, path, sizeof(tmp) - 1);
    tmp[sizeof(tmp) - 1] = '\0';
    
    for (char *p = tmp + strlen(ALLOWED_DIR) + 1; *p; p++) {
        if (*p == '/') {
            *p = '\0';
            mkdir(tmp, 0755);
            *p = '/';
        }
    }
}

/* ============================================================
   CONFIRMATION PROMPTS
   ============================================================ */

static bool get_confirmation(const char *action, const char *path, const char *detail) {
    printf("\n");
    printf("╔══════════════════════════════════════════════════════════════╗\n");
    printf("║  CONFIRMATION REQUIRED                                       ║\n");
    printf("╠══════════════════════════════════════════════════════════════╣\n");
    printf("║  Action: %-52s ║\n", action);
    printf("║  Path:   %-52s ║\n", path);
    if (detail && detail[0]) {
        char truncated[50];
        strncpy(truncated, detail, sizeof(truncated) - 4);
        truncated[sizeof(truncated) - 4] = '\0';
        if (strlen(detail) > sizeof(truncated) - 4) {
            strcat(truncated, "...");
        }
        printf("║  Detail: %-52s ║\n", truncated);
    }
    printf("╚══════════════════════════════════════════════════════════════╝\n");
    printf("\nProceed? [y/N]: ");
    fflush(stdout);
    
    char response[16];
    if (!fgets(response, sizeof(response), stdin)) {
        return false;
    }
    
    size_t len = strlen(response);
    if (len > 0 && response[len - 1] == '\n') {
        response[len - 1] = '\0';
    }
    
    bool confirmed = (response[0] == 'y' || response[0] == 'Y');
    
    if (!confirmed) {
        printf("Operation cancelled.\n");
        log_write(LOG_INFO, "User declined confirmation for %s on %s", action, path);
    }
    
    return confirmed;
}

/* ============================================================
   FILE OPERATIONS
   ============================================================ */

typedef struct {
    bool success;
    char message[512];
    char *file_content;  /* For read operations, return content to add to context */
} OpResult;

static OpResult do_list(const char *rel_path) {
    OpResult result = {false, "", NULL};
    char full[MAX_PATH_LEN];
    
    if (!rel_path || !rel_path[0] || strcmp(rel_path, ".") == 0) {
        strncpy(full, ALLOWED_DIR, sizeof(full) - 1);
    } else if (!safe_path(rel_path, full, sizeof(full))) {
        snprintf(result.message, sizeof(result.message), "Invalid path");
        return result;
    }
    
    DIR *dir = opendir(full);
    if (!dir) {
        snprintf(result.message, sizeof(result.message), 
                 "Cannot open directory: %s", strerror(errno));
        return result;
    }
    
    /* Build listing string for context */
    char listing[4096] = "";
    size_t listing_len = 0;
    
    printf("\nContents of %s:\n", full);
    printf("────────────────────────────────────────\n");
    
    struct dirent *entry;
    int count = 0;
    while ((entry = readdir(dir))) {
        if (entry->d_name[0] != '.') {
            const char *type_indicator = "";
            if (entry->d_type == DT_DIR) type_indicator = "/";
            else if (entry->d_type == DT_LNK) type_indicator = "@";
            
            printf("  %s%s\n", entry->d_name, type_indicator);
            
            /* Add to listing string */
            int written = snprintf(listing + listing_len, sizeof(listing) - listing_len,
                                   "%s%s\n", entry->d_name, type_indicator);
            if (written > 0 && listing_len + written < sizeof(listing)) {
                listing_len += written;
            }
            count++;
        }
    }
    
    if (count == 0) {
        printf("  (empty)\n");
        strcpy(listing, "(empty directory)");
    }
    printf("────────────────────────────────────────\n");
    printf("Total: %d items\n", count);
    
    closedir(dir);
    
    result.success = true;
    result.file_content = strdup(listing);
    snprintf(result.message, sizeof(result.message), "Listed %d items", count);
    return result;
}

static OpResult do_read(const char *rel_path) {
    OpResult result = {false, "", NULL};
    char full[MAX_PATH_LEN];
    
    if (!safe_path(rel_path, full, sizeof(full))) {
        snprintf(result.message, sizeof(result.message), "Invalid path");
        return result;
    }
    
    FILE *f = fopen(full, "r");
    if (!f) {
        snprintf(result.message, sizeof(result.message), 
                 "Cannot read file: %s", strerror(errno));
        return result;
    }
    
    fseek(f, 0, SEEK_END);
    long size = ftell(f);
    fseek(f, 0, SEEK_SET);
    
    /* Read entire file into buffer */
    char *content = (char *)malloc(size + 1);
    if (!content) {
        fclose(f);
        snprintf(result.message, sizeof(result.message), "Out of memory");
        return result;
    }
    
    size_t read_size = fread(content, 1, size, f);
    content[read_size] = '\0';
    fclose(f);
    
    printf("\nContents of %s (%ld bytes):\n", rel_path, size);
    printf("────────────────────────────────────────\n");
    printf("%s", content);
    printf("\n────────────────────────────────────────\n");
    
    result.success = true;
    result.file_content = content;  /* Transfer ownership */
    snprintf(result.message, sizeof(result.message), "Read %ld bytes", size);
    return result;
}

static OpResult do_write(const char *rel_path, const char *content, bool append, bool skip_confirm) {
    OpResult result = {false, "", NULL};
    char full[MAX_PATH_LEN];
    
    if (!safe_path(rel_path, full, sizeof(full))) {
        snprintf(result.message, sizeof(result.message), "Invalid path");
        return result;
    }
    
    bool needs_confirm = append ? CONFIRM_APPEND : CONFIRM_WRITE;
    if (needs_confirm && !skip_confirm) {
        const char *action_name = append ? "APPEND" : "WRITE";
        char detail[128];
        snprintf(detail, sizeof(detail), "%zu bytes", strlen(content));
        if (!get_confirmation(action_name, rel_path, detail)) {
            snprintf(result.message, sizeof(result.message), "Cancelled by user");
            return result;
        }
    }
    
    ensure_parent_dirs(full);
    
    FILE *f = fopen(full, append ? "a" : "w");
    if (!f) {
        snprintf(result.message, sizeof(result.message), 
                 "Cannot write file: %s", strerror(errno));
        return result;
    }
    
    size_t written = fwrite(content, 1, strlen(content), f);
    fclose(f);
    
    result.success = true;
    snprintf(result.message, sizeof(result.message), 
             "%s %zu bytes to %s", 
             append ? "Appended" : "Wrote", written, rel_path);
    
    printf("%s\n", result.message);
    return result;
}

static OpResult do_delete(const char *rel_path, bool skip_confirm) {
    OpResult result = {false, "", NULL};
    char full[MAX_PATH_LEN];
    
    if (!safe_path(rel_path, full, sizeof(full))) {
        snprintf(result.message, sizeof(result.message), "Invalid path");
        return result;
    }
    
    struct stat st;
    if (stat(full, &st) != 0) {
        snprintf(result.message, sizeof(result.message), 
                 "File not found: %s", rel_path);
        return result;
    }
    
    if (S_ISDIR(st.st_mode)) {
        snprintf(result.message, sizeof(result.message), 
                 "Cannot delete directories: %s", rel_path);
        return result;
    }
    
    if (CONFIRM_DELETE && !skip_confirm) {
        char detail[64];
        snprintf(detail, sizeof(detail), "Size: %lld bytes", (long long)st.st_size);
        if (!get_confirmation("DELETE", rel_path, detail)) {
            snprintf(result.message, sizeof(result.message), "Cancelled by user");
            return result;
        }
    }
    
    if (remove(full) == 0) {
        result.success = true;
        snprintf(result.message, sizeof(result.message), "Deleted: %s", rel_path);
        printf("%s\n", result.message);
    } else {
        snprintf(result.message, sizeof(result.message), 
                 "Delete failed: %s", strerror(errno));
    }
    
    return result;
}

/* ============================================================
   OLLAMA API (Chat endpoint with context)
   ============================================================ */

static const char *SYSTEM_PROMPT = 
    "You are a file assistant with access to a sandboxed directory.\n"
    "\n"
    "RESPOND ONLY with a single valid JSON object. No markdown, no explanation, no code blocks.\n"
    "\n"
    "JSON format:\n"
    "{\"action\": \"ACTION\", \"path\": \"relative/path\", \"content\": \"text\"}\n"
    "\n"
    "Valid actions:\n"
    "- list: List directory contents (path=\".\" for root)\n"
    "- read: Read file contents (I will show you the contents)\n"  
    "- write: Create/overwrite file (provide full content)\n"
    "- append: Add to end of file\n"
    "- delete: Remove a file\n"
    "\n"
    "IMPORTANT for editing files:\n"
    "1. First use \"read\" to see current contents\n"
    "2. I will show you the file contents\n"
    "3. Then use \"write\" with the COMPLETE modified content\n"
    "\n"
    "Rules:\n"
    "- Return ONLY valid JSON, nothing else\n"
    "- For HTML content, use proper tags like <html>, <head>, <body>\n"
    "- Always include all three fields: action, path, content\n"
    "- For read/list/delete, set content to empty string\n";

static bool call_ollama(char *response_out, size_t response_size) {
    CURL *curl = curl_easy_init();
    if (!curl) {
        log_write(LOG_ERROR, "Failed to initialize curl");
        return false;
    }
    
    ResponseBuffer chunk = {0};
    
    /* Build the messages array using cJSON */
    cJSON *request = cJSON_CreateObject();
    cJSON_AddStringToObject(request, "model", MODEL_NAME);
    cJSON_AddBoolToObject(request, "stream", false);
    
    cJSON *messages = cJSON_CreateArray();
    
    /* Add system message */
    cJSON *sys_msg = cJSON_CreateObject();
    cJSON_AddStringToObject(sys_msg, "role", "system");
    cJSON_AddStringToObject(sys_msg, "content", SYSTEM_PROMPT);
    cJSON_AddItemToArray(messages, sys_msg);
    
    /* Add conversation history */
    for (int i = 0; i < g_conversation.count; i++) {
        cJSON *msg = cJSON_CreateObject();
        cJSON_AddStringToObject(msg, "role", g_conversation.messages[i].role);
        cJSON_AddStringToObject(msg, "content", g_conversation.messages[i].content);
        cJSON_AddItemToArray(messages, msg);
    }
    
    cJSON_AddItemToObject(request, "messages", messages);
    
    char *post_data = cJSON_PrintUnformatted(request);
    cJSON_Delete(request);
    
    if (!post_data) {
        log_write(LOG_ERROR, "Failed to create request JSON");
        curl_easy_cleanup(curl);
        return false;
    }
    
    curl_easy_setopt(curl, CURLOPT_URL, OLLAMA_URL);
    curl_easy_setopt(curl, CURLOPT_POST, 1L);
    curl_easy_setopt(curl, CURLOPT_POSTFIELDS, post_data);
    curl_easy_setopt(curl, CURLOPT_WRITEFUNCTION, write_callback);
    curl_easy_setopt(curl, CURLOPT_WRITEDATA, &chunk);
    curl_easy_setopt(curl, CURLOPT_TIMEOUT, 120L);
    
    struct curl_slist *headers = NULL;
    headers = curl_slist_append(headers, "Content-Type: application/json");
    curl_easy_setopt(curl, CURLOPT_HTTPHEADER, headers);
    
    log_write(LOG_INFO, "Sending request to Ollama (chat endpoint)...");
    
    CURLcode res = curl_easy_perform(curl);
    
    free(post_data);
    curl_slist_free_all(headers);
    curl_easy_cleanup(curl);
    
    if (res != CURLE_OK) {
        log_write(LOG_ERROR, "Curl error: %s", curl_easy_strerror(res));
        free(chunk.data);
        return false;
    }
    
    if (!chunk.data) {
        log_write(LOG_ERROR, "No response from Ollama");
        return false;
    }
    
    /* Parse Ollama's chat response */
    cJSON *ollama_response = cJSON_Parse(chunk.data);
    if (!ollama_response) {
        log_write(LOG_ERROR, "Failed to parse Ollama response as JSON");
        log_write(LOG_ERROR, "Raw response: %.500s", chunk.data);
        free(chunk.data);
        return false;
    }
    
    /* Extract message.content from chat response */
    cJSON *message = cJSON_GetObjectItemCaseSensitive(ollama_response, "message");
    if (!message) {
        log_write(LOG_ERROR, "No 'message' field in Ollama output");
        cJSON_Delete(ollama_response);
        free(chunk.data);
        return false;
    }
    
    cJSON *content = cJSON_GetObjectItemCaseSensitive(message, "content");
    if (!cJSON_IsString(content) || !content->valuestring) {
        log_write(LOG_ERROR, "No 'content' in message");
        cJSON_Delete(ollama_response);
        free(chunk.data);
        return false;
    }
    
    strncpy(response_out, content->valuestring, response_size - 1);
    response_out[response_size - 1] = '\0';
    
    cJSON_Delete(ollama_response);
    free(chunk.data);
    
    return true;
}

/* ============================================================
   COMMAND PARSING AND EXECUTION
   ============================================================ */

typedef struct {
    char action[32];
    char path[MAX_PATH_LEN];
    char *content;  /* Heap allocated for large content */
    bool valid;
} Command;

static void command_free(Command *cmd) {
    if (cmd->content) {
        free(cmd->content);
        cmd->content = NULL;
    }
}

static Command parse_command(const char *json_str) {
    Command cmd = {0};
    cmd.valid = false;
    
    /* Skip any leading whitespace or markdown code blocks */
    const char *start = json_str;
    while (*start && (*start == ' ' || *start == '\n' || *start == '\t' || *start == '`')) {
        start++;
    }
    
    /* Find the JSON object */
    const char *json_start = strchr(start, '{');
    if (!json_start) {
        log_write(LOG_WARN, "No JSON object found in response");
        return cmd;
    }
    
    cJSON *json = cJSON_Parse(json_start);
    if (!json) {
        log_write(LOG_WARN, "Failed to parse command JSON: %s", 
                  cJSON_GetErrorPtr() ? cJSON_GetErrorPtr() : "unknown error");
        return cmd;
    }
    
    cJSON *action = cJSON_GetObjectItemCaseSensitive(json, "action");
    if (!cJSON_IsString(action) || !action->valuestring) {
        log_write(LOG_WARN, "Missing or invalid 'action' field");
        cJSON_Delete(json);
        return cmd;
    }
    strncpy(cmd.action, action->valuestring, sizeof(cmd.action) - 1);
    
    cJSON *path = cJSON_GetObjectItemCaseSensitive(json, "path");
    if (cJSON_IsString(path) && path->valuestring) {
        strncpy(cmd.path, path->valuestring, sizeof(cmd.path) - 1);
    }
    
    cJSON *content = cJSON_GetObjectItemCaseSensitive(json, "content");
    if (cJSON_IsString(content) && content->valuestring) {
        cmd.content = strdup(content->valuestring);
    } else {
        cmd.content = strdup("");
    }
    
    cJSON_Delete(json);
    cmd.valid = true;
    
    return cmd;
}

static void execute_command(Command *cmd, const char *user_input, 
                           const char *model_response) {
    OpResult result = {false, "Unknown action", NULL};
    bool confirmed = false;
    
    if (strcmp(cmd->action, "list") == 0) {
        result = do_list(cmd->path);
        
        /* Add result to conversation context */
        if (result.success && result.file_content) {
            char context_msg[4096];
            snprintf(context_msg, sizeof(context_msg),
                     "Directory listing for '%s':\n%s", 
                     cmd->path[0] ? cmd->path : ".", result.file_content);
            conversation_add("user", context_msg);
        }
    } 
    else if (strcmp(cmd->action, "read") == 0) {
        result = do_read(cmd->path);
        
        /* Add file contents to conversation context - THIS IS KEY */
        if (result.success && result.file_content) {
            char context_msg[MAX_CONTENT];
            snprintf(context_msg, sizeof(context_msg),
                     "Contents of '%s':\n```\n%s\n```\n\nYou can now modify this file using the 'write' action with the complete new content.",
                     cmd->path, result.file_content);
            conversation_add("user", context_msg);
            
            printf("\n[File contents added to conversation context]\n");
        }
    } 
    else if (strcmp(cmd->action, "write") == 0) {
        result = do_write(cmd->path, cmd->content, false, false);
        confirmed = result.success;
    } 
    else if (strcmp(cmd->action, "append") == 0) {
        result = do_write(cmd->path, cmd->content, true, false);
        confirmed = result.success;
    } 
    else if (strcmp(cmd->action, "delete") == 0) {
        result = do_delete(cmd->path, false);
        confirmed = result.success;
    } 
    else {
        log_write(LOG_WARN, "Unknown action: %s", cmd->action);
        printf("Unknown action: %s\n", cmd->action);
    }
    
    /* Clean up file content if allocated */
    if (result.file_content) {
        free(result.file_content);
    }
    
    log_audit(user_input, model_response, cmd->action, cmd->path, 
              result.message, confirmed);
}

/* ============================================================
   UTILITY FUNCTIONS
   ============================================================ */

static void show_recent_logs(void) {
    FILE *f = fopen(LOG_FILE, "r");
    if (!f) {
        printf("No log file found.\n");
        return;
    }
    
    char lines[50][512];
    int line_count = 0;
    int current_line = 0;
    
    while (fgets(lines[current_line], sizeof(lines[0]), f)) {
        current_line = (current_line + 1) % 50;
        if (line_count < 50) line_count++;
    }
    fclose(f);
    
    printf("\n═══ Recent Log Entries ═══\n");
    int start = (line_count < 50) ? 0 : current_line;
    for (int i = 0; i < line_count; i++) {
        int idx = (start + i) % 50;
        printf("%s", lines[idx]);
    }
    printf("══════════════════════════\n\n");
}

static void show_context(void) {
    printf("\n═══ Conversation Context (%d messages) ═══\n", g_conversation.count);
    for (int i = 0; i < g_conversation.count; i++) {
        printf("[%d] %s: %.100s%s\n", i, 
               g_conversation.messages[i].role,
               g_conversation.messages[i].content,
               strlen(g_conversation.messages[i].content) > 100 ? "..." : "");
    }
    printf("═══════════════════════════════════════════\n\n");
}

static void print_banner(void) {
    printf("\n");
    printf("╔═══════════════════════════════════════════════════════════════╗\n");
    printf("║           FILE AGENT v2 FOR OLLAMA                            ║\n");
    printf("║           (with conversation context)                         ║\n");
    printf("╠═══════════════════════════════════════════════════════════════╣\n");
    printf("║  Model:   %-50s   ║\n", MODEL_NAME);
    
    char sandbox_abs[MAX_PATH_LEN];
    if (realpath(ALLOWED_DIR, sandbox_abs)) {
        printf("║  Sandbox: %-50s   ║\n", sandbox_abs);
    } else {
        printf("║  Sandbox: %-50s   ║\n", ALLOWED_DIR);
    }
    
    printf("║  Log:     %-50s   ║\n", LOG_FILE);
    printf("╠═══════════════════════════════════════════════════════════════╣\n");
    printf("║  Commands:                                                    ║\n");
    printf("║    - Natural language file operations                         ║\n");
    printf("║    - 'log' or 'logs' - view recent log entries                ║\n");
    printf("║    - 'context' - view conversation history                    ║\n");
    printf("║    - 'clear' - reset conversation context                     ║\n");
    printf("║    - 'quit' or 'exit' - stop                                  ║\n");
    printf("╚═══════════════════════════════════════════════════════════════╝\n");
    printf("\n");
    printf("TIP: To edit a file, first ask to read it. The contents will be\n");
    printf("     added to context, then ask for your modifications.\n");
    printf("\n");
}

/* ============================================================
   MAIN
   ============================================================ */

int main(void) {
    mkdir(ALLOWED_DIR, 0755);
    log_init();
    curl_global_init(CURL_GLOBAL_DEFAULT);
    
    print_banner();
    
    log_write(LOG_INFO, "Model: %s, Sandbox: %s", MODEL_NAME, ALLOWED_DIR);
    
    char user_input[2048];
    char model_response[MAX_CONTENT];
    
    while (1) {
        printf("You: ");
        fflush(stdout);
        
        if (!fgets(user_input, sizeof(user_input), stdin)) {
            printf("\nExiting.\n");
            break;
        }
        
        size_t len = strlen(user_input);
        if (len > 0 && user_input[len - 1] == '\n') {
            user_input[len - 1] = '\0';
            len--;
        }
        
        if (len == 0) continue;
        
        /* Built-in commands */
        if (strcmp(user_input, "quit") == 0 || 
            strcmp(user_input, "exit") == 0 ||
            strcmp(user_input, "q") == 0) {
            break;
        }
        
        if (strcmp(user_input, "log") == 0 || strcmp(user_input, "logs") == 0) {
            show_recent_logs();
            continue;
        }
        
        if (strcmp(user_input, "context") == 0) {
            show_context();
            continue;
        }
        
        if (strcmp(user_input, "clear") == 0) {
            conversation_clear();
            printf("Conversation context cleared.\n\n");
            continue;
        }
        
        log_write(LOG_INFO, "User input: %s", user_input);
        
        /* Add user message to conversation */
        conversation_add("user", user_input);
        
        /* Call Ollama */
        printf("Thinking...\n");
        if (!call_ollama(model_response, sizeof(model_response))) {
            printf("Failed to get response from model.\n\n");
            continue;
        }
        
        printf("Model: %s\n", model_response);
        log_write(LOG_INFO, "Model response: %s", model_response);
        
        /* Add assistant response to conversation */
        conversation_add("assistant", model_response);
        
        /* Parse the command */
        Command cmd = parse_command(model_response);
        if (!cmd.valid) {
            printf("Could not parse model's response as a valid command.\n");
            printf("(The model may need clearer instructions. Try rephrasing.)\n\n");
            continue;
        }
        
        execute_command(&cmd, user_input, model_response);
        command_free(&cmd);
        printf("\n");
    }
    
    conversation_clear();
    curl_global_cleanup();
    log_close();
    
    printf("Goodbye.\n");
    return 0;
}

/*
 * file_agent.c
 * 
 * Minimal file agent for Ollama models with:
 *   - cJSON for robust JSON parsing
 *   - Confirmation prompts for destructive operations
 *   - Comprehensive audit logging
 *
 * Compile: gcc file_agent.c cJSON.c -o file_agent -lcurl
 * Run:     ./file_agent
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
#include "cJSON.h"

/* ============================================================
   CONFIGURATION
   ============================================================ */

#define ALLOWED_DIR     "./sandbox"
#define MODEL_NAME      "qwen2.5-coder:7b"
#define OLLAMA_URL      "http://localhost:11434/api/generate"
#define LOG_FILE        "./file_agent.log"
#define MAX_CONTENT     65536
#define MAX_PATH_LEN    1024

/* Operations that require confirmation */
#define CONFIRM_WRITE   1
#define CONFIRM_DELETE  1
#define CONFIRM_APPEND  0  /* Set to 1 if you want append confirmation too */

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
    fprintf(g_log_file, "[%s] [INFO] File Agent Started\n", timestamp);
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
    
    /* Always print errors and warnings to stderr */
    if (level == LOG_ERROR || level == LOG_WARN) {
        va_list args;
        va_start(args, format);
        fprintf(stderr, "[%s] ", log_level_str(level));
        vfprintf(stderr, format, args);
        fprintf(stderr, "\n");
        va_end(args);
    }
    
    /* Write to log file if open */
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

/* Structured audit log entry */
static void log_audit(const char *user_input, const char *model_response,
                      const char *action, const char *path, 
                      const char *result, bool confirmed) {
    if (!g_log_file) return;
    
    char timestamp[64];
    get_timestamp(timestamp, sizeof(timestamp));
    
    fprintf(g_log_file, "\n--- AUDIT ENTRY ---\n");
    fprintf(g_log_file, "Timestamp: %s\n", timestamp);
    fprintf(g_log_file, "User Input: %s\n", user_input);
    fprintf(g_log_file, "Model Response: %s\n", model_response);
    fprintf(g_log_file, "Action: %s\n", action);
    fprintf(g_log_file, "Path: %s\n", path);
    fprintf(g_log_file, "Confirmed: %s\n", confirmed ? "YES" : "NO/N/A");
    fprintf(g_log_file, "Result: %s\n", result);
    fprintf(g_log_file, "-------------------\n");
    fflush(g_log_file);
}

/* Need va_list for variable arguments */
#include <stdarg.h>

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
   JSON HELPERS (using cJSON)
   ============================================================ */

/* Build a JSON-escaped string for embedding in request */
static char *json_escape_string(const char *src) {
    /* cJSON provides this functionality */
    cJSON *str = cJSON_CreateString(src);
    if (!str) return NULL;
    
    char *printed = cJSON_PrintUnformatted(str);
    cJSON_Delete(str);
    
    /* printed includes quotes, we may want to strip them depending on use */
    return printed;
}

/* ============================================================
   PATH SAFETY
   ============================================================ */

static bool safe_path(const char *relative, char *out, size_t out_size) {
    /* Block empty paths */
    if (!relative || relative[0] == '\0') {
        log_write(LOG_WARN, "Security: Empty path rejected");
        return false;
    }
    
    /* Block absolute paths */
    if (relative[0] == '/') {
        log_write(LOG_WARN, "Security: Absolute path rejected: %s", relative);
        return false;
    }
    
    /* Block path traversal */
    if (strstr(relative, "..")) {
        log_write(LOG_WARN, "Security: Path traversal blocked: %s", relative);
        return false;
    }
    
    /* Build full path */
    char full[MAX_PATH_LEN];
    int written = snprintf(full, sizeof(full), "%s/%s", ALLOWED_DIR, relative);
    if (written >= (int)sizeof(full)) {
        log_write(LOG_WARN, "Security: Path too long: %s", relative);
        return false;
    }
    
    /* Additional check: resolve and verify containment */
    char sandbox_real[MAX_PATH_LEN];
    char *resolved_sandbox = realpath(ALLOWED_DIR, sandbox_real);
    
    if (resolved_sandbox) {
        /* For existing files, check real path */
        char full_real[MAX_PATH_LEN];
        char *resolved_full = realpath(full, full_real);
        
        if (resolved_full) {
            /* File exists - verify it's under sandbox */
            if (strncmp(full_real, sandbox_real, strlen(sandbox_real)) != 0) {
                log_write(LOG_WARN, "Security: Resolved path escapes sandbox: %s -> %s", 
                          relative, full_real);
                return false;
            }
        }
        /* If file doesn't exist yet, we rely on the .. check above */
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
        /* Truncate detail for display */
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
    
    /* Strip newline and check */
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
    char message[256];
} OpResult;

static OpResult do_list(const char *rel_path) {
    OpResult result = {false, ""};
    char full[MAX_PATH_LEN];
    
    /* Default to sandbox root */
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
            count++;
        }
    }
    
    if (count == 0) {
        printf("  (empty)\n");
    }
    printf("────────────────────────────────────────\n");
    printf("Total: %d items\n", count);
    
    closedir(dir);
    
    result.success = true;
    snprintf(result.message, sizeof(result.message), "Listed %d items", count);
    return result;
}

static OpResult do_read(const char *rel_path) {
    OpResult result = {false, ""};
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
    
    /* Get file size */
    fseek(f, 0, SEEK_END);
    long size = ftell(f);
    fseek(f, 0, SEEK_SET);
    
    printf("\nContents of %s (%ld bytes):\n", rel_path, size);
    printf("────────────────────────────────────────\n");
    
    char buf[4096];
    size_t n;
    while ((n = fread(buf, 1, sizeof(buf) - 1, f)) > 0) {
        buf[n] = '\0';
        printf("%s", buf);
    }
    
    printf("\n────────────────────────────────────────\n");
    
    fclose(f);
    
    result.success = true;
    snprintf(result.message, sizeof(result.message), "Read %ld bytes", size);
    return result;
}

static OpResult do_write(const char *rel_path, const char *content, bool append, bool confirmed) {
    OpResult result = {false, ""};
    char full[MAX_PATH_LEN];
    
    if (!safe_path(rel_path, full, sizeof(full))) {
        snprintf(result.message, sizeof(result.message), "Invalid path");
        return result;
    }
    
    /* Check if confirmation is required */
    bool needs_confirm = append ? CONFIRM_APPEND : CONFIRM_WRITE;
    if (needs_confirm && !confirmed) {
        const char *action_name = append ? "APPEND" : "WRITE";
        if (!get_confirmation(action_name, rel_path, content)) {
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

static OpResult do_delete(const char *rel_path, bool confirmed) {
    OpResult result = {false, ""};
    char full[MAX_PATH_LEN];
    
    if (!safe_path(rel_path, full, sizeof(full))) {
        snprintf(result.message, sizeof(result.message), "Invalid path");
        return result;
    }
    
    /* Check if file exists */
    struct stat st;
    if (stat(full, &st) != 0) {
        snprintf(result.message, sizeof(result.message), 
                 "File not found: %s", rel_path);
        return result;
    }
    
    /* Don't delete directories (safety measure) */
    if (S_ISDIR(st.st_mode)) {
        snprintf(result.message, sizeof(result.message), 
                 "Cannot delete directories: %s", rel_path);
        return result;
    }
    
    /* Confirmation required for delete */
    if (CONFIRM_DELETE && !confirmed) {
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
   OLLAMA API
   ============================================================ */

static const char *SYSTEM_PROMPT = 
    "You are a file assistant with access to a sandboxed directory.\n"
    "Respond ONLY with a single JSON object in this exact format:\n"
    "{\"action\": \"read\", \"path\": \"relative/path\", \"content\": \"\"}\n"
    "\n"
    "Valid actions:\n"
    "- list: List contents of a directory. Use path \".\" for root.\n"
    "- read: Read a file's contents\n"
    "- write: Create or overwrite a file (content required)\n"
    "- append: Add to end of a file (content required)\n"
    "- delete: Remove a file\n"
    "\n"
    "Rules:\n"
    "1. Return ONLY valid JSON, no explanations\n"
    "2. The JSON must be on a single line\n"
    "3. Always include all three fields: action, path, content\n"
    "4. For read/list/delete, set content to empty string\n";

static bool call_ollama(const char *user_input, char *response_out, size_t response_size) {
    CURL *curl = curl_easy_init();
    if (!curl) {
        log_write(LOG_ERROR, "Failed to initialize curl");
        return false;
    }
    
    ResponseBuffer chunk = {0};
    
    /* Build the request JSON using cJSON for safety */
    cJSON *request = cJSON_CreateObject();
    cJSON_AddStringToObject(request, "model", MODEL_NAME);
    cJSON_AddBoolToObject(request, "stream", false);
    
    /* Combine system prompt and user input */
    char full_prompt[8192];
    snprintf(full_prompt, sizeof(full_prompt), 
             "%s\n\nUser request: %s", SYSTEM_PROMPT, user_input);
    cJSON_AddStringToObject(request, "prompt", full_prompt);
    
    char *post_data = cJSON_PrintUnformatted(request);
    cJSON_Delete(request);
    
    if (!post_data) {
        log_write(LOG_ERROR, "Failed to create request JSON");
        curl_easy_cleanup(curl);
        return false;
    }
    
    /* Set curl options */
    curl_easy_setopt(curl, CURLOPT_URL, OLLAMA_URL);
    curl_easy_setopt(curl, CURLOPT_POST, 1L);
    curl_easy_setopt(curl, CURLOPT_POSTFIELDS, post_data);
    curl_easy_setopt(curl, CURLOPT_WRITEFUNCTION, write_callback);
    curl_easy_setopt(curl, CURLOPT_WRITEDATA, &chunk);
    curl_easy_setopt(curl, CURLOPT_TIMEOUT, 120L);  /* 2 minute timeout */
    
    struct curl_slist *headers = NULL;
    headers = curl_slist_append(headers, "Content-Type: application/json");
    curl_easy_setopt(curl, CURLOPT_HTTPHEADER, headers);
    
    log_write(LOG_INFO, "Sending request to Ollama...");
    
    /* Execute request */
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
    
    /* Parse Ollama's response using cJSON */
    cJSON *ollama_response = cJSON_Parse(chunk.data);
    if (!ollama_response) {
        log_write(LOG_ERROR, "Failed to parse Ollama response as JSON");
        log_write(LOG_ERROR, "Raw response: %s", chunk.data);
        free(chunk.data);
        return false;
    }
    
    /* Extract the "response" field */
    cJSON *response_field = cJSON_GetObjectItemCaseSensitive(ollama_response, "response");
    if (!cJSON_IsString(response_field) || !response_field->valuestring) {
        log_write(LOG_ERROR, "No 'response' field in Ollama output");
        cJSON_Delete(ollama_response);
        free(chunk.data);
        return false;
    }
    
    strncpy(response_out, response_field->valuestring, response_size - 1);
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
    char content[MAX_CONTENT];
    bool valid;
} Command;

static Command parse_command(const char *json_str) {
    Command cmd = {0};
    cmd.valid = false;
    
    cJSON *json = cJSON_Parse(json_str);
    if (!json) {
        log_write(LOG_WARN, "Failed to parse command JSON: %s", 
                  cJSON_GetErrorPtr() ? cJSON_GetErrorPtr() : "unknown error");
        return cmd;
    }
    
    /* Extract action */
    cJSON *action = cJSON_GetObjectItemCaseSensitive(json, "action");
    if (!cJSON_IsString(action) || !action->valuestring) {
        log_write(LOG_WARN, "Missing or invalid 'action' field");
        cJSON_Delete(json);
        return cmd;
    }
    strncpy(cmd.action, action->valuestring, sizeof(cmd.action) - 1);
    
    /* Extract path */
    cJSON *path = cJSON_GetObjectItemCaseSensitive(json, "path");
    if (cJSON_IsString(path) && path->valuestring) {
        strncpy(cmd.path, path->valuestring, sizeof(cmd.path) - 1);
    }
    
    /* Extract content */
    cJSON *content = cJSON_GetObjectItemCaseSensitive(json, "content");
    if (cJSON_IsString(content) && content->valuestring) {
        strncpy(cmd.content, content->valuestring, sizeof(cmd.content) - 1);
    }
    
    cJSON_Delete(json);
    cmd.valid = true;
    
    return cmd;
}

static void execute_command(const Command *cmd, const char *user_input, 
                           const char *model_response) {
    OpResult result = {false, "Unknown action"};
    bool confirmed = false;
    
    if (strcmp(cmd->action, "list") == 0) {
        result = do_list(cmd->path);
    } 
    else if (strcmp(cmd->action, "read") == 0) {
        result = do_read(cmd->path);
    } 
    else if (strcmp(cmd->action, "write") == 0) {
        /* Confirmation handled inside do_write */
        result = do_write(cmd->path, cmd->content, false, false);
        confirmed = result.success;  /* If successful, user confirmed */
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
    
    /* Write audit log entry */
    log_audit(user_input, model_response, cmd->action, cmd->path, 
              result.message, confirmed);
}

/* ============================================================
   MAIN
   ============================================================ */

static void print_banner(void) {
    printf("\n");
    printf("╔═══════════════════════════════════════════════════════════════╗\n");
    printf("║              FILE AGENT FOR OLLAMA                            ║\n");
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
    printf("║  Commands: Natural language file operations                   ║\n");
    printf("║  Type 'quit' or 'exit' to stop                                ║\n");
    printf("║  Type 'log' to view recent log entries                        ║\n");
    printf("╚═══════════════════════════════════════════════════════════════╝\n");
    printf("\n");
}

static void show_recent_logs(void) {
    FILE *f = fopen(LOG_FILE, "r");
    if (!f) {
        printf("No log file found.\n");
        return;
    }
    
    /* Read last 50 lines */
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

int main(void) {
    /* Create sandbox if needed */
    mkdir(ALLOWED_DIR, 0755);
    
    /* Initialize logging */
    log_init();
    
    /* Initialize curl globally */
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
        
        /* Strip trailing newline */
        size_t len = strlen(user_input);
        if (len > 0 && user_input[len - 1] == '\n') {
            user_input[len - 1] = '\0';
            len--;
        }
        
        /* Skip empty input */
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
        
        log_write(LOG_INFO, "User input: %s", user_input);
        
        /* Call Ollama */
        printf("Thinking...\n");
        if (!call_ollama(user_input, model_response, sizeof(model_response))) {
            printf("Failed to get response from model.\n\n");
            continue;
        }
        
        printf("Model: %s\n", model_response);
        log_write(LOG_INFO, "Model response: %s", model_response);
        
        /* Parse the command */
        Command cmd = parse_command(model_response);
        if (!cmd.valid) {
            printf("Could not parse model's response as a valid command.\n\n");
            log_write(LOG_WARN, "Invalid command from model");
            continue;
        }
        
        /* Execute with audit logging */
        execute_command(&cmd, user_input, model_response);
        printf("\n");
    }
    
    curl_global_cleanup();
    log_close();
    
    printf("Goodbye.\n");
    return 0;
}

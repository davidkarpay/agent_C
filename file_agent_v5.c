/*
 * file_agent_v5.c - FIXED
 *
 * Compile: gcc file_agent_v5.c cJSON.c -o file_agent -lcurl
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
   HTML REPAIR - FIXED VERSION
   ============================================================
   
   The model outputs ? instead of < and >
   
   Rules:
   1. ?/ is ALWAYS </ (start of closing tag)
   2. ?! is ALWAYS <! (DOCTYPE or comment)
   3. Otherwise: closing context → >, opening context → <
*/
static char *repair_html(const char *input) {
    if (!input) return NULL;
    
    size_t len = strlen(input);
    char *output = malloc(len + 1);
    if (!output) return NULL;
    
    size_t j = 0;
    
    for (size_t i = 0; i < len; i++) {
        char c = input[i];
        
        if (c != '?') {
            output[j++] = c;
            continue;
        }
        
        char prev = (i > 0) ? input[i - 1] : '\0';
        char next = (i + 1 < len) ? input[i + 1] : '\0';
        
        /* SPECIAL CASE: ?/ is ALWAYS the start of a closing tag </  */
        if (next == '/') {
            output[j++] = '<';
            continue;
        }
        
        /* SPECIAL CASE: ?! is ALWAYS start of <!DOCTYPE or <!-- */
        if (next == '!') {
            output[j++] = '<';
            continue;
        }
        
        /* Does next char suggest we're starting a tag? */
        bool opening = (
            (next >= 'a' && next <= 'z') ||
            (next >= 'A' && next <= 'Z')
        );
        
        /* Does previous char suggest we're at end of a tag? */
        bool closing = (
            (prev >= 'a' && prev <= 'z') ||
            (prev >= 'A' && prev <= 'Z') ||
            (prev >= '0' && prev <= '9') ||
            prev == '"' ||
            prev == '\'' ||
            prev == '/' ||
            prev == '-' ||
            prev == ']'
        );
        
        /* Decide based on context */
        if (closing) {
            output[j++] = '>';
        } else if (opening) {
            output[j++] = '<';
        } else {
            output[j++] = '?';
        }
    }
    
    output[j] = '\0';
    return output;
}

/* Test the repair function - strings split to avoid trigraph warnings */
static void test_repair(void) {
    printf("\n=== HTML Repair Test ===\n\n");
    
    struct { const char *in; const char *expected; } tests[] = {
        {"?html?", "<html>"},
        {"?/html?", "</html>"},
        {"?html?" "?/html?", "<html></html>"},  /* Split to avoid trigraph */
        {"?h1?Hello?/h1?", "<h1>Hello</h1>"},
        {"?!DOCTYPE html?", "<!DOCTYPE html>"},
        {"?div class=\"test\"?content?/div?", "<div class=\"test\">content</div>"},
        {"?p?Hello World?/p?", "<p>Hello World</p>"},
        {"?br/?", "<br/>"},
        {"?style?body { color: red; }?/style?", "<style>body { color: red; }</style>"},
        {"?a href=\"#\"?Link?/a?", "<a href=\"#\">Link</a>"},
        {"?!-- comment --?", "<!-- comment -->"},
        {"?script?alert('hi');?/script?", "<script>alert('hi');</script>"},
        {NULL, NULL}
    };
    
    int passed = 0, failed = 0;
    
    for (int i = 0; tests[i].in; i++) {
        char *result = repair_html(tests[i].in);
        bool ok = result && strcmp(result, tests[i].expected) == 0;
        
        printf("%s Test %d:\n", ok ? "✓" : "✗", i + 1);
        printf("  IN:       %s\n", tests[i].in);
        printf("  EXPECTED: %s\n", tests[i].expected);
        printf("  GOT:      %s\n\n", result ? result : "(null)");
        
        if (ok) passed++; else failed++;
        free(result);
    }
    
    printf("Results: %d passed, %d failed\n", passed, failed);
    printf("========================\n\n");
}

/* ============================================================
   CONVERSATION HISTORY
   ============================================================ */

typedef struct { char role[16]; char *content; } Message;
typedef struct { Message messages[MAX_HISTORY]; int count; } Conversation;
static Conversation g_conv = {0};

static void conv_clear(void) {
    for (int i = 0; i < g_conv.count; i++) free(g_conv.messages[i].content);
    g_conv.count = 0;
}

static void conv_add(const char *role, const char *content) {
    if (g_conv.count >= MAX_HISTORY) {
        free(g_conv.messages[0].content);
        memmove(&g_conv.messages[0], &g_conv.messages[1], sizeof(Message) * (MAX_HISTORY - 1));
        g_conv.count--;
    }
    int idx = g_conv.count++;
    strncpy(g_conv.messages[idx].role, role, 15);
    g_conv.messages[idx].content = strdup(content);
}

/* ============================================================
   LOGGING
   ============================================================ */

static FILE *g_log = NULL;

static void log_open(void) {
    g_log = fopen(LOG_FILE, "a");
    if (g_log) {
        time_t now = time(NULL);
        fprintf(g_log, "\n=== Session %s", ctime(&now));
    }
}

static void log_close(void) { if (g_log) fclose(g_log); g_log = NULL; }

static void logf(const char *fmt, ...) {
    if (!g_log) return;
    va_list args;
    va_start(args, fmt);
    vfprintf(g_log, fmt, args);
    fprintf(g_log, "\n");
    fflush(g_log);
    va_end(args);
}

/* ============================================================
   CURL
   ============================================================ */

typedef struct { char *data; size_t size; } Buffer;

static size_t curl_cb(void *p, size_t sz, size_t n, void *u) {
    Buffer *b = u;
    size_t len = sz * n;
    b->data = realloc(b->data, b->size + len + 1);
    if (!b->data) return 0;
    memcpy(b->data + b->size, p, len);
    b->size += len;
    b->data[b->size] = 0;
    return len;
}

/* ============================================================
   FILE OPERATIONS
   ============================================================ */

static bool safe_path(const char *rel, char *out, size_t sz) {
    if (!rel || !rel[0] || rel[0] == '/' || strstr(rel, "..")) return false;
    snprintf(out, sz, "%s/%s", ALLOWED_DIR, rel);
    return true;
}

static void mkdirs(const char *path) {
    char tmp[MAX_PATH_LEN];
    strncpy(tmp, path, sizeof(tmp) - 1);
    for (char *p = tmp + strlen(ALLOWED_DIR) + 1; *p; p++) {
        if (*p == '/') { *p = 0; mkdir(tmp, 0755); *p = '/'; }
    }
}

static char *file_read(const char *rel, long *sz) {
    char full[MAX_PATH_LEN];
    if (!safe_path(rel, full, sizeof(full))) return NULL;
    FILE *f = fopen(full, "r");
    if (!f) return NULL;
    fseek(f, 0, SEEK_END);
    *sz = ftell(f);
    fseek(f, 0, SEEK_SET);
    char *buf = malloc(*sz + 1);
    if (buf) { fread(buf, 1, *sz, f); buf[*sz] = 0; }
    fclose(f);
    return buf;
}

static bool file_write(const char *rel, const char *content, bool append) {
    char full[MAX_PATH_LEN];
    if (!safe_path(rel, full, sizeof(full))) return false;
    mkdirs(full);
    FILE *f = fopen(full, append ? "a" : "w");
    if (!f) return false;
    fwrite(content, 1, strlen(content), f);
    fclose(f);
    return true;
}

static bool file_delete(const char *rel) {
    char full[MAX_PATH_LEN];
    if (!safe_path(rel, full, sizeof(full))) return false;
    return remove(full) == 0;
}

static char *file_list(const char *rel) {
    char full[MAX_PATH_LEN];
    if (!rel || !rel[0] || strcmp(rel, ".") == 0) {
        strncpy(full, ALLOWED_DIR, sizeof(full));
    } else if (!safe_path(rel, full, sizeof(full))) {
        return NULL;
    }
    DIR *d = opendir(full);
    if (!d) return NULL;
    char *out = malloc(4096);
    if (!out) { closedir(d); return NULL; }
    out[0] = 0;
    size_t len = 0;
    struct dirent *e;
    while ((e = readdir(d))) {
        if (e->d_name[0] != '.') {
            len += snprintf(out + len, 4096 - len, "  %s%s\n",
                           e->d_name, e->d_type == DT_DIR ? "/" : "");
        }
    }
    closedir(d);
    return out;
}

/* ============================================================
   OLLAMA API
   ============================================================ */

static const char *SYS_PROMPT =
"You are a file assistant. Return ONLY a JSON object.\n"
"\n"
"Format: {\"action\": \"ACTION\", \"path\": \"PATH\", \"content\": \"CONTENT\"}\n"
"\n"
"Actions:\n"
"- list: List files in a directory\n"
"- read: READ and DISPLAY a file (DO NOT write, just read it)\n"
"- write: Create or overwrite a file with new content\n"
"- append: Add text to end of existing file\n"
"- delete: Remove a file\n"
"\n"
"IMPORTANT RULES:\n"
"- When user says 'read', 'show', 'display', 'view', 'cat', 'open' → use action \"read\"\n"
"- When user says 'create', 'write', 'make', 'save' → use action \"write\"\n"
"- For 'read' action: content MUST be empty string \"\"\n"
"- For 'write' action: content contains the file contents\n"
"\n"
"Return ONLY the JSON object, no explanations.";

static bool call_ollama(char *resp, size_t resp_sz) {
    CURL *curl = curl_easy_init();
    if (!curl) return false;
    
    Buffer buf = {0};
    
    cJSON *req = cJSON_CreateObject();
    cJSON_AddStringToObject(req, "model", MODEL_NAME);
    cJSON_AddBoolToObject(req, "stream", false);
    cJSON_AddStringToObject(req, "format", "json");
    
    cJSON *msgs = cJSON_CreateArray();
    cJSON *sys = cJSON_CreateObject();
    cJSON_AddStringToObject(sys, "role", "system");
    cJSON_AddStringToObject(sys, "content", SYS_PROMPT);
    cJSON_AddItemToArray(msgs, sys);
    
    for (int i = 0; i < g_conv.count; i++) {
        cJSON *m = cJSON_CreateObject();
        cJSON_AddStringToObject(m, "role", g_conv.messages[i].role);
        cJSON_AddStringToObject(m, "content", g_conv.messages[i].content);
        cJSON_AddItemToArray(msgs, m);
    }
    
    cJSON_AddItemToObject(req, "messages", msgs);
    
    char *post = cJSON_PrintUnformatted(req);
    cJSON_Delete(req);
    
    curl_easy_setopt(curl, CURLOPT_URL, OLLAMA_URL);
    curl_easy_setopt(curl, CURLOPT_POST, 1L);
    curl_easy_setopt(curl, CURLOPT_POSTFIELDS, post);
    curl_easy_setopt(curl, CURLOPT_WRITEFUNCTION, curl_cb);
    curl_easy_setopt(curl, CURLOPT_WRITEDATA, &buf);
    curl_easy_setopt(curl, CURLOPT_TIMEOUT, 180L);
    
    struct curl_slist *h = curl_slist_append(NULL, "Content-Type: application/json");
    curl_easy_setopt(curl, CURLOPT_HTTPHEADER, h);
    
    CURLcode res = curl_easy_perform(curl);
    free(post);
    curl_slist_free_all(h);
    curl_easy_cleanup(curl);
    
    if (res != CURLE_OK || !buf.data) { free(buf.data); return false; }
    
    cJSON *r = cJSON_Parse(buf.data);
    free(buf.data);
    if (!r) return false;
    
    cJSON *msg = cJSON_GetObjectItem(r, "message");
    cJSON *content = msg ? cJSON_GetObjectItem(msg, "content") : NULL;
    if (!cJSON_IsString(content)) { cJSON_Delete(r); return false; }
    
    strncpy(resp, content->valuestring, resp_sz - 1);
    cJSON_Delete(r);
    return true;
}

/* ============================================================
   COMMAND HANDLING
   ============================================================ */

typedef struct {
    char action[32];
    char path[MAX_PATH_LEN];
    char *content;
    char *content_fixed;
    bool valid;
} Command;

static Command parse_cmd(const char *json_str) {
    Command cmd = {0};
    
    cJSON *json = cJSON_Parse(json_str);
    if (!json) return cmd;
    
    cJSON *action = cJSON_GetObjectItem(json, "action");
    cJSON *path = cJSON_GetObjectItem(json, "path");
    cJSON *content = cJSON_GetObjectItem(json, "content");
    
    if (!cJSON_IsString(action)) { cJSON_Delete(json); return cmd; }
    
    strncpy(cmd.action, action->valuestring, sizeof(cmd.action) - 1);
    if (cJSON_IsString(path)) strncpy(cmd.path, path->valuestring, sizeof(cmd.path) - 1);
    
    if (cJSON_IsString(content) && content->valuestring[0]) {
        cmd.content = strdup(content->valuestring);
        cmd.content_fixed = repair_html(content->valuestring);
    } else {
        cmd.content = strdup("");
        cmd.content_fixed = strdup("");
    }
    
    cJSON_Delete(json);
    cmd.valid = true;
    return cmd;
}

static void cmd_free(Command *cmd) {
    free(cmd->content);
    free(cmd->content_fixed);
    cmd->content = cmd->content_fixed = NULL;
}

static bool confirm_write(const char *action, const char *path, const char *content) {
    size_t len = strlen(content);
    
    printf("\n┌─────────────────────────────────────────────────────────────────┐\n");
    printf("│ %s: %s (%zu bytes)\n", action, path, len);
    printf("├─────────────────────────────────────────────────────────────────┤\n");
    
    /* Show preview */
    const char *p = content;
    int lines = 0;
    while (*p && lines < 20) {
        printf("│ ");
        int col = 0;
        while (*p && *p != '\n' && col < 63) {
            putchar(*p++);
            col++;
        }
        if (*p == '\n') p++;
        printf("\n");
        lines++;
    }
    if (*p) printf("│ ... (%zu more bytes)\n", strlen(p));
    
    printf("└─────────────────────────────────────────────────────────────────┘\n");
    printf("Write this content? [y/N]: ");
    fflush(stdout);
    
    char resp[16];
    if (!fgets(resp, sizeof(resp), stdin)) return false;
    return (resp[0] == 'y' || resp[0] == 'Y');
}

static void run_cmd(Command *cmd) {
    logf("ACTION: %s PATH: %s", cmd->action, cmd->path);
    
    if (strcmp(cmd->action, "list") == 0) {
        char *list = file_list(cmd->path);
        if (list) {
            printf("\n📁 %s:\n%s", cmd->path[0] ? cmd->path : ".", list);
            char ctx[4096];
            snprintf(ctx, sizeof(ctx), "Files:\n%s", list);
            conv_add("assistant", ctx);
            free(list);
        } else {
            printf("❌ Cannot list\n");
        }
    }
    else if (strcmp(cmd->action, "read") == 0) {
        long sz;
        char *content = file_read(cmd->path, &sz);
        if (content) {
            printf("\n📄 %s (%ld bytes):\n", cmd->path, sz);
            printf("────────────────────────────────────────\n");
            printf("%s\n", content);
            printf("────────────────────────────────────────\n");
            
            char *ctx = malloc(sz + 256);
            if (ctx) {
                snprintf(ctx, sz + 256, "File %s:\n```\n%s\n```", cmd->path, content);
                conv_add("assistant", ctx);
                free(ctx);
            }
            printf("✓ Loaded into context\n");
            free(content);
        } else {
            printf("❌ Cannot read %s\n", cmd->path);
        }
    }
    else if (strcmp(cmd->action, "write") == 0) {
        if (!cmd->content_fixed || !cmd->content_fixed[0]) {
            printf("❌ No content\n");
            return;
        }
        
        /* Show if repair happened */
        if (strcmp(cmd->content, cmd->content_fixed) != 0) {
            printf("\n🔧 HTML tags repaired (? → < >)\n");
        }
        
        if (!CONFIRM_WRITE || confirm_write("WRITE", cmd->path, cmd->content_fixed)) {
            if (file_write(cmd->path, cmd->content_fixed, false)) {
                printf("✓ Wrote %zu bytes to %s\n", strlen(cmd->content_fixed), cmd->path);
                logf("WROTE %zu bytes to %s", strlen(cmd->content_fixed), cmd->path);
            } else {
                printf("❌ Write failed\n");
            }
        } else {
            printf("Cancelled\n");
        }
    }
    else if (strcmp(cmd->action, "append") == 0) {
        if (file_write(cmd->path, cmd->content_fixed, true)) {
            printf("✓ Appended to %s\n", cmd->path);
        } else {
            printf("❌ Append failed\n");
        }
    }
    else if (strcmp(cmd->action, "delete") == 0) {
        printf("⚠️  Delete %s? [y/N]: ", cmd->path);
        fflush(stdout);
        char resp[16];
        if (fgets(resp, sizeof(resp), stdin) && (resp[0] == 'y' || resp[0] == 'Y')) {
            if (file_delete(cmd->path)) printf("✓ Deleted\n");
            else printf("❌ Failed\n");
        } else {
            printf("Cancelled\n");
        }
    }
    else {
        printf("❓ Unknown: %s\n", cmd->action);
    }
}

/* ============================================================
   MAIN
   ============================================================ */

int main(int argc, char **argv) {
    if (argc > 1 && strcmp(argv[1], "--test") == 0) {
        test_repair();
        return 0;
    }
    
    mkdir(ALLOWED_DIR, 0755);
    log_open();
    curl_global_init(CURL_GLOBAL_DEFAULT);
    
    printf("\n");
    printf("╔═══════════════════════════════════════════════════════════════╗\n");
    printf("║           FILE AGENT v5 (HTML repair fixed)                   ║\n");
    printf("╠═══════════════════════════════════════════════════════════════╣\n");
    printf("║  Model: %-53s  ║\n", MODEL_NAME);
    char abs[MAX_PATH_LEN];
    printf("║  Dir:   %-53s  ║\n", realpath(ALLOWED_DIR, abs) ? abs : ALLOWED_DIR);
    printf("╠═══════════════════════════════════════════════════════════════╣\n");
    printf("║  quit | log | context | clear | help                          ║\n");
    printf("╚═══════════════════════════════════════════════════════════════╝\n\n");
    
    char input[2048], response[MAX_CONTENT];
    
    while (1) {
        printf("You: ");
        fflush(stdout);
        
        if (!fgets(input, sizeof(input), stdin)) break;
        size_t len = strlen(input);
        if (len && input[len-1] == '\n') input[--len] = 0;
        if (!len) continue;
        
        if (strcmp(input, "quit") == 0) break;
        if (strcmp(input, "help") == 0) {
            printf("\nlist, read <file>, create <file>, delete <file>\n");
            printf("To edit: read first, then describe changes\n\n");
            continue;
        }
        if (strcmp(input, "clear") == 0) { conv_clear(); printf("✓ Cleared\n\n"); continue; }
        if (strcmp(input, "context") == 0) {
            printf("\n[%d msgs]\n", g_conv.count);
            for (int i = 0; i < g_conv.count; i++)
                printf("%s: %.50s...\n", g_conv.messages[i].role, g_conv.messages[i].content);
            printf("\n");
            continue;
        }
        if (strcmp(input, "log") == 0) {
            FILE *f = fopen(LOG_FILE, "r");
            if (f) { char ln[256]; while(fgets(ln,256,f)) printf("%s",ln); fclose(f); }
            continue;
        }
        
        conv_add("user", input);
        logf("USER: %s", input);
        
        printf("🤔 ...\n");
        
        if (!call_ollama(response, sizeof(response))) {
            printf("❌ Model error\n\n");
            continue;
        }
        
        logf("MODEL: %s", response);
        printf("Model: %s\n", response);
        
        Command cmd = parse_cmd(response);
        if (!cmd.valid) { printf("❌ Parse error\n\n"); continue; }
        
        run_cmd(&cmd);
        cmd_free(&cmd);
        printf("\n");
    }
    
    conv_clear();
    curl_global_cleanup();
    log_close();
    printf("Bye!\n");
    return 0;
}

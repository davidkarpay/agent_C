/*
 * test_repair.c - Standalone test for HTML repair function
 * Compile: gcc test_repair.c -o test_repair
 */

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdbool.h>

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

int main(void) {
    printf("\n=== HTML Repair Test ===\n\n");
    
    struct { const char *in; const char *expected; } tests[] = {
        {"?html?", "<html>"},
        {"?/html?", "</html>"},
        {"?html?" "?/html?", "<html></html>"},
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
    
    return failed > 0 ? 1 : 0;
}

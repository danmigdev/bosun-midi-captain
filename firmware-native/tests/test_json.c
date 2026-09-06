#include "bosun/json.h"
#include <assert.h>
#include <stdio.h>
#include <string.h>

int main(void) {
    bosun_json_token_t tokens[128]; bosun_json_doc_t d; char out[128]; int32_t number; bool boolean;
    const char *json = "{\"id\":\"q1\",\"nested\":[{},[1,2],{\"x\":true}],\"label\":\"wah \\u00e8 \\ud83c\\udfb8\",\"n\":-2147483648}";
    assert(bosun_json_parse(&d, json, strlen(json), tokens, 128) == BOSUN_JSON_OK);
    assert(bosun_json_equal(&d, bosun_json_get(&d, 0, "id"), "q1"));
    assert(bosun_json_string(&d, bosun_json_get(&d, 0, "label"), out, sizeof out));
    assert(strcmp(out, "wah \xc3\xa8 \xf0\x9f\x8e\xb8") == 0);
    assert(bosun_json_integer(&d, bosun_json_get(&d, 0, "n"), &number) && number == INT32_MIN);
    int object = bosun_json_at(&d, bosun_json_get(&d, 0, "nested"), 2);
    assert(bosun_json_boolean(&d, bosun_json_get(&d, object, "x"), &boolean) && boolean);
    assert(bosun_json_get(&d, 0, "missing") == -1);
    assert(!bosun_json_string(&d, bosun_json_get(&d, 0, "label"), out, 3));
    const char *bad[] = {"", "[1,]", "{\"x\":1,}", "{\"x\" 1}", "01", "1e", "1.", "true false", "\"\\x\"", "\"\\u01\"", "\"\xc0\x80\"", "[", "{1:2}", "\"a\n\""};
    for (unsigned i = 0; i < sizeof bad / sizeof *bad; ++i)
        assert(bosun_json_parse(&d, bad[i], strlen(bad[i]), tokens, 128) == BOSUN_JSON_INVALID);
    const char nul_escape[] = {'"', '\\', 0, '"'};
    assert(bosun_json_parse(&d, nul_escape, sizeof nul_escape, tokens, 128) == BOSUN_JSON_INVALID);
    assert(d.count == 0);
    assert(bosun_json_parse(&d, "[1,2,3]", 7, tokens, 2) == BOSUN_JSON_LIMIT);
    char deep[81]; memset(deep, '[', 40); memset(deep + 40, ']', 40); deep[80] = 0;
    assert(bosun_json_parse(&d, deep, 80, tokens, 128) == BOSUN_JSON_LIMIT);
    const char *bounds[] = {"2147483648", "-2147483649", "1.0", "1e2"};
    for (unsigned i = 0; i < 4; ++i) {
        assert(bosun_json_parse(&d, bounds[i], strlen(bounds[i]), tokens, 128) == BOSUN_JSON_OK);
        assert(!bosun_json_integer(&d, 0, &number));
    }
    bosun_json_writer_t w; bosun_json_writer_init(&w, out, sizeof out);
    assert(bosun_json_puts(&w, "{\"name\":"));
    assert(bosun_json_quote(&w, "a\"b\n\t"));
    assert(bosun_json_puts(&w, ",\"n\":"));
    assert(bosun_json_write_integer(&w, INT32_MIN)); assert(bosun_json_puts(&w, "}"));
    assert(bosun_json_parse(&d, out, w.length, tokens, 128) == BOSUN_JSON_OK);
    assert(bosun_json_equal(&d, bosun_json_get(&d, 0, "name"), "a\"b\n\t"));
    char tiny[3]; bosun_json_writer_init(&w, tiny, sizeof tiny);
    assert(!bosun_json_quote(&w, "abc") && w.failed && tiny[2] == '\0');
    assert(!bosun_json_puts(&w, "x"));
    puts("JSON: strict syntax, UTF-8, bounded depth/tokens/writer and integer limits passed");
    return 0;
}

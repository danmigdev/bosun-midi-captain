#include "bosun/json.h"
#include <limits.h>
#include <stdio.h>
#include <string.h>

typedef struct { bosun_json_doc_t *doc; size_t pos; bosun_json_result_t error; } parser_t;
static void whitespace(parser_t *p) {
    while (p->pos < p->doc->length) {
        char c = p->doc->text[p->pos];
        if (c != ' ' && c != '\t' && c != '\r' && c != '\n') break;
        ++p->pos;
    }
}
static int hex(char c) {
    if (c >= '0' && c <= '9') return c - '0';
    if (c >= 'a' && c <= 'f') return c - 'a' + 10;
    if (c >= 'A' && c <= 'F') return c - 'A' + 10;
    return -1;
}
static bool utf8(const char *s, size_t n, size_t *used) {
    const unsigned char c = (unsigned char)s[0];
    unsigned count; uint32_t value, minimum;
    if (c < 0x80) { *used = 1; return true; }
    if (c >= 0xc2 && c <= 0xdf) { count = 2; value = c & 31u; minimum = 0x80; }
    else if (c >= 0xe0 && c <= 0xef) { count = 3; value = c & 15u; minimum = 0x800; }
    else if (c >= 0xf0 && c <= 0xf4) { count = 4; value = c & 7u; minimum = 0x10000; }
    else return false;
    if (n < count) return false;
    for (unsigned i = 1; i < count; ++i) {
        unsigned char b = (unsigned char)s[i];
        if ((b & 0xc0u) != 0x80u) return false;
        value = (value << 6) | (b & 63u);
    }
    if (value < minimum || value > 0x10ffff || (value >= 0xd800 && value <= 0xdfff)) return false;
    *used = count; return true;
}
static bool string_end(parser_t *p) {
    ++p->pos;
    while (p->pos < p->doc->length) {
        unsigned char c = (unsigned char)p->doc->text[p->pos++];
        if (c == '"') return true;
        if (c < 32) return false;
        if (c == '\\') {
            if (p->pos == p->doc->length) return false;
            c = (unsigned char)p->doc->text[p->pos++];
            if (c == 'u') {
                for (unsigned i = 0; i < 4; ++i)
                    if (p->pos == p->doc->length || hex(p->doc->text[p->pos++]) < 0) return false;
            } else if (!c || !strchr("\"\\/bfnrt", c)) return false;
        } else if (c >= 0x80) {
            size_t bytes;
            if (!utf8(p->doc->text + p->pos - 1, p->doc->length - p->pos + 1, &bytes)) return false;
            p->pos += bytes - 1;
        }
    }
    return false;
}
static bool digit(char c) { return c >= '0' && c <= '9'; }
static bool number_end(parser_t *p) {
    const char *s = p->doc->text; size_t n = p->doc->length;
    if (s[p->pos] == '-') { if (++p->pos == n) return false; }
    if (s[p->pos] == '0') ++p->pos;
    else {
        if (s[p->pos] < '1' || s[p->pos] > '9') return false;
        while (p->pos < n && digit(s[p->pos])) ++p->pos;
    }
    if (p->pos < n && s[p->pos] == '.') {
        if (++p->pos == n || !digit(s[p->pos])) return false;
        while (p->pos < n && digit(s[p->pos])) ++p->pos;
    }
    if (p->pos < n && (s[p->pos] == 'e' || s[p->pos] == 'E')) {
        if (++p->pos == n) return false;
        if (s[p->pos] == '-' || s[p->pos] == '+') ++p->pos;
        if (p->pos == n || !digit(s[p->pos])) return false;
        while (p->pos < n && digit(s[p->pos])) ++p->pos;
    }
    return true;
}
static bool value(parser_t *p, unsigned depth) {
    whitespace(p);
    if (depth > 32 || p->doc->count == p->doc->capacity) { p->error = BOSUN_JSON_LIMIT; return false; }
    if (p->pos == p->doc->length) return false;
    const unsigned index = p->doc->count++;
    bosun_json_token_t *t = &p->doc->tokens[index];
    t->start = (uint32_t)p->pos; t->reserved = 0;
    char c = p->doc->text[p->pos];
    if (c == '{' || c == '[') {
        bool object = c == '{'; char close = object ? '}' : ']';
        t->type = (uint8_t)(object ? BOSUN_JSON_OBJECT : BOSUN_JSON_ARRAY);
        ++p->pos; whitespace(p);
        if (p->pos < p->doc->length && p->doc->text[p->pos] != close) {
            while (true) {
                if (object) {
                    whitespace(p);
                    if (p->pos == p->doc->length || p->doc->text[p->pos] != '"' || !value(p, depth + 1)) return false;
                    whitespace(p);
                    if (p->pos == p->doc->length || p->doc->text[p->pos++] != ':') return false;
                }
                if (!value(p, depth + 1)) return false;
                whitespace(p);
                if (p->pos == p->doc->length) return false;
                if (p->doc->text[p->pos] != ',') break;
                ++p->pos;
            }
        }
        if (p->pos == p->doc->length || p->doc->text[p->pos++] != close) return false;
    } else if (c == '"') {
        t->type = BOSUN_JSON_STRING;
        if (!string_end(p)) return false;
    } else if (c == '-' || digit(c)) {
        t->type = BOSUN_JSON_NUMBER;
        if (!number_end(p)) return false;
    } else {
        const char *literal;
        if (c == 't') { literal = "true"; t->type = BOSUN_JSON_TRUE; }
        else if (c == 'f') { literal = "false"; t->type = BOSUN_JSON_FALSE; }
        else if (c == 'n') { literal = "null"; t->type = BOSUN_JSON_NULL; }
        else return false;
        size_t length = strlen(literal);
        if (length > p->doc->length - p->pos || memcmp(p->doc->text + p->pos, literal, length)) return false;
        p->pos += length;
    }
    t->end = (uint32_t)p->pos; t->next = p->doc->count;
    return true;
}
bosun_json_result_t bosun_json_parse(bosun_json_doc_t *doc, const char *text, size_t length,
                                    bosun_json_token_t *tokens, uint16_t capacity) {
    if (!doc || !text || !tokens || !capacity || length > UINT32_MAX) return BOSUN_JSON_INVALID;
    *doc = (bosun_json_doc_t){text, length, tokens, 0, capacity};
    parser_t p = {doc, 0, BOSUN_JSON_INVALID};
    if (!value(&p, 0)) { doc->count = 0; return p.error; }
    whitespace(&p);
    if (p.pos != length) { doc->count = 0; return BOSUN_JSON_INVALID; }
    return BOSUN_JSON_OK;
}
static bool valid(const bosun_json_doc_t *d, int token) {
    return d && token >= 0 && (unsigned)token < d->count;
}
bool bosun_json_raw(const bosun_json_doc_t *d, int token, const char **data, size_t *length) {
    if (!valid(d, token)) return false;
    *data = d->text + d->tokens[token].start;
    *length = d->tokens[token].end - d->tokens[token].start;
    return true;
}
static uint32_t unicode4(const char *s) {
    uint32_t n = 0;
    for (unsigned i = 0; i < 4; ++i) n = (n << 4) | (unsigned)hex(s[i]);
    return n;
}
static bool decoded(const bosun_json_doc_t *d, int token, char *out, size_t capacity, const char *compare) {
    if (!valid(d, token) || d->tokens[token].type != BOSUN_JSON_STRING || (!compare && !capacity)) return false;
    size_t pos = d->tokens[token].start + 1, end = d->tokens[token].end - 1, length = 0;
    while (pos < end) {
        unsigned char bytes[4]; size_t count = 1;
        bytes[0] = (unsigned char)d->text[pos++];
        if (bytes[0] == '\\') {
            char c = d->text[pos++];
            if (c == 'u') {
                uint32_t code = unicode4(d->text + pos); pos += 4;
                if (code >= 0xd800 && code <= 0xdbff) {
                    if (end - pos < 6 || d->text[pos] != '\\' || d->text[pos + 1] != 'u') return false;
                    uint32_t low = unicode4(d->text + pos + 2);
                    if (low < 0xdc00 || low > 0xdfff) return false;
                    code = 0x10000u + ((code - 0xd800u) << 10) + low - 0xdc00u; pos += 6;
                } else if (code >= 0xdc00 && code <= 0xdfff) return false;
                if (!code) return false; /* C strings cannot represent embedded NUL. */
                if (code < 0x80) bytes[0] = (unsigned char)code;
                else if (code < 0x800) { count = 2; bytes[0] = (unsigned char)(0xc0u | (code >> 6)); bytes[1] = (unsigned char)(0x80u | (code & 63u)); }
                else if (code < 0x10000) { count = 3; bytes[0] = (unsigned char)(0xe0u | (code >> 12)); bytes[1] = (unsigned char)(0x80u | ((code >> 6) & 63u)); bytes[2] = (unsigned char)(0x80u | (code & 63u)); }
                else { count = 4; bytes[0] = (unsigned char)(0xf0u | (code >> 18)); bytes[1] = (unsigned char)(0x80u | ((code >> 12) & 63u)); bytes[2] = (unsigned char)(0x80u | ((code >> 6) & 63u)); bytes[3] = (unsigned char)(0x80u | (code & 63u)); }
            } else {
                switch (c) { case 'b': bytes[0] = '\b'; break; case 'f': bytes[0] = '\f'; break;
                    case 'n': bytes[0] = '\n'; break; case 'r': bytes[0] = '\r'; break;
                    case 't': bytes[0] = '\t'; break; default: bytes[0] = (unsigned char)c; break; }
            }
        }
        for (size_t i = 0; i < count; ++i) {
            if (compare) { if (!compare[length] || (unsigned char)compare[length] != bytes[i]) return false; }
            else { if (length + 1 >= capacity) { out[0] = '\0'; return false; } out[length] = (char)bytes[i]; }
            ++length;
        }
    }
    if (compare) return compare[length] == '\0';
    out[length] = '\0'; return true;
}
bool bosun_json_string(const bosun_json_doc_t *d, int token, char *out, size_t capacity) {
    return out && decoded(d, token, out, capacity, NULL);
}
bool bosun_json_equal(const bosun_json_doc_t *d, int token, const char *value_text) {
    return value_text && decoded(d, token, NULL, 0, value_text);
}
int bosun_json_get(const bosun_json_doc_t *d, int object, const char *key) {
    if (!valid(d, object) || d->tokens[object].type != BOSUN_JSON_OBJECT) return -1;
    int found = -1;
    for (unsigned i = (unsigned)object + 1; i < d->tokens[object].next;) {
        unsigned v = i + 1;
        if (bosun_json_equal(d, (int)i, key)) found = (int)v;
        i = d->tokens[v].next;
    }
    return found;
}
int bosun_json_at(const bosun_json_doc_t *d, int array, unsigned index) {
    if (!valid(d, array) || d->tokens[array].type != BOSUN_JSON_ARRAY) return -1;
    for (unsigned i = (unsigned)array + 1; i < d->tokens[array].next; i = d->tokens[i].next)
        if (index-- == 0) return (int)i;
    return -1;
}
bool bosun_json_integer(const bosun_json_doc_t *d, int token, int32_t *out) {
    if (!out || !valid(d, token) || d->tokens[token].type != BOSUN_JSON_NUMBER) return false;
    size_t i = d->tokens[token].start, end = d->tokens[token].end; bool negative = d->text[i] == '-';
    if (negative) ++i;
    uint32_t value_number = 0, limit = negative ? UINT32_C(2147483648) : INT32_MAX;
    for (; i < end; ++i) {
        if (!digit(d->text[i])) return false;
        uint32_t n = (uint32_t)(d->text[i] - '0');
        if (value_number > (limit - n) / 10u) return false;
        value_number = value_number * 10u + n;
    }
    *out = negative ? (value_number == UINT32_C(2147483648) ? INT32_MIN : -(int32_t)value_number) : (int32_t)value_number;
    return true;
}
bool bosun_json_boolean(const bosun_json_doc_t *d, int token, bool *out) {
    if (!out || !valid(d, token)) return false;
    if (d->tokens[token].type == BOSUN_JSON_TRUE) { *out = true; return true; }
    if (d->tokens[token].type == BOSUN_JSON_FALSE) { *out = false; return true; }
    return false;
}
void bosun_json_writer_init(bosun_json_writer_t *w, char *buffer, size_t capacity) {
    *w = (bosun_json_writer_t){buffer, 0, capacity, capacity == 0};
    if (capacity) buffer[0] = '\0';
}
bool bosun_json_write(bosun_json_writer_t *w, const char *data, size_t length) {
    if (w->failed || length >= w->capacity - w->length) { w->failed = true; return false; }
    memcpy(w->data + w->length, data, length); w->length += length; w->data[w->length] = '\0'; return true;
}
bool bosun_json_puts(bosun_json_writer_t *w, const char *text) { return bosun_json_write(w, text, strlen(text)); }
bool bosun_json_quote(bosun_json_writer_t *w, const char *text) {
    static const char digits[] = "0123456789abcdef";
    if (!bosun_json_puts(w, "\"")) return false;
    for (const unsigned char *p = (const unsigned char *)text; *p; ++p) {
        char c = (char)*p;
        if (c == '"' || c == '\\') { if (!bosun_json_puts(w, "\\")) return false; }
        else if (*p < 32) {
            char escape[6] = {'\\', 'u', '0', '0', digits[*p >> 4], digits[*p & 15]};
            if (!bosun_json_write(w, escape, sizeof escape)) return false;
            continue;
        }
        if (!bosun_json_write(w, &c, 1)) return false;
    }
    return bosun_json_puts(w, "\"");
}
bool bosun_json_write_integer(bosun_json_writer_t *w, int32_t value_number) {
    char number[16]; int n = snprintf(number, sizeof number, "%ld", (long)value_number);
    return n > 0 && (size_t)n < sizeof number && bosun_json_write(w, number, (size_t)n);
}

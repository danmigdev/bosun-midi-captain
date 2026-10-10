#ifndef BOSUN_JSON_H
#define BOSUN_JSON_H
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

typedef enum {
    BOSUN_JSON_OBJECT, BOSUN_JSON_ARRAY, BOSUN_JSON_STRING,
    BOSUN_JSON_NUMBER, BOSUN_JSON_TRUE, BOSUN_JSON_FALSE, BOSUN_JSON_NULL
} bosun_json_type_t;

/* Tokens refer to immutable input and include their complete raw JSON range.
 * next is the first token after all children; no allocation or linked nodes. */
typedef struct {
    uint32_t start, end;
    uint16_t next;
    uint8_t type, reserved;
} bosun_json_token_t;
typedef struct {
    const char *text;
    size_t length;
    bosun_json_token_t *tokens;
    uint16_t count, capacity;
} bosun_json_doc_t;

typedef enum { BOSUN_JSON_OK, BOSUN_JSON_INVALID, BOSUN_JSON_LIMIT } bosun_json_result_t;
bosun_json_result_t bosun_json_parse(bosun_json_doc_t *doc, const char *text,
                                    size_t length, bosun_json_token_t *tokens,
                                    uint16_t capacity);
int bosun_json_get(const bosun_json_doc_t *doc, int object, const char *key);
int bosun_json_at(const bosun_json_doc_t *doc, int array, unsigned index);
bool bosun_json_equal(const bosun_json_doc_t *doc, int token, const char *value);
bool bosun_json_string(const bosun_json_doc_t *doc, int token, char *out, size_t capacity);
bool bosun_json_integer(const bosun_json_doc_t *doc, int token, int32_t *value);
bool bosun_json_boolean(const bosun_json_doc_t *doc, int token, bool *value);
bool bosun_json_raw(const bosun_json_doc_t *doc, int token, const char **data, size_t *length);

/* Bounded builder for small responses. Overflow is sticky and never produces
 * a success response with silently truncated JSON. Large data is streamed. */
typedef struct { char *data; size_t length, capacity; bool failed; } bosun_json_writer_t;
void bosun_json_writer_init(bosun_json_writer_t *w, char *buffer, size_t capacity);
bool bosun_json_write(bosun_json_writer_t *w, const char *data, size_t length);
bool bosun_json_puts(bosun_json_writer_t *w, const char *text);
bool bosun_json_quote(bosun_json_writer_t *w, const char *text);
bool bosun_json_write_integer(bosun_json_writer_t *w, int32_t value);
#endif

/* Standalone memory-boundary test; compile on Linux, never opens a display. */
#define main framebuffer_program_main
#include "../boot-splash/framebuffer.c"
#undef main
#include <assert.h>

int main(void)
{
    unsigned char memory[80];
    memset(memory, 0xa5, sizeof(memory));
    unsigned char rgb[] = {10, 20, 30, 255, 0, 0};
    struct picture pic = {2, 1, rgb};
    struct fb_fix_screeninfo fix = {.type = FB_TYPE_PACKED_PIXELS,
        .visual = FB_VISUAL_TRUECOLOR, .line_length = 20};
    struct fb_var_screeninfo var = {.xres = 4, .yres = 3, .bits_per_pixel = 32,
        .red = {16, 8, 0}, .green = {8, 8, 0}, .blue = {0, 8, 0}, .transp = {24, 8, 0}};
    assert(paint(memory, sizeof(memory), &fix, &var, &pic) == 0);
    assert(memory[0] == 30 && memory[1] == 20 && memory[2] == 10 && memory[3] == 255);
    assert(memory[28] == 0 && memory[29] == 0 && memory[30] == 255);
    for (unsigned int row = 0; row < 3; ++row)
        for (unsigned int byte = 16; byte < 20; ++byte) assert(memory[row * 20 + byte] == 0xa5);
    for (unsigned int i = 60; i < sizeof(memory); ++i) assert(memory[i] == 0xa5);
    memset(memory, 0xa5, sizeof(memory));
    assert(paint(memory, 40, &fix, &var, &pic) == -1);
    var.yoffset = UINT32_MAX;
    assert(paint(memory, sizeof(memory), &fix, &var, &pic) == -1);
    for (unsigned int i = 0; i < sizeof(memory); ++i) assert(memory[i] == 0xa5);
    var.yoffset = 0;
    var.xres = 1; /* cropping must read the image safely */
    assert(paint(memory, sizeof(memory), &fix, &var, &pic) == 0);
    puts("framebuffer pixels, clipping and bounds: OK");
    return 0;
}

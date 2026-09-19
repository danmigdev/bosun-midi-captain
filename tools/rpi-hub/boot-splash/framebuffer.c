/* Draw once into an existing framebuffer. Never set a mode, switch VT, or own DRM. */
#include <errno.h>
#include <fcntl.h>
#include <linux/fb.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/mman.h>
#include <unistd.h>

struct picture { uint32_t width, height; unsigned char *rgb; };

static uint32_t little32(const unsigned char *p)
{
    return (uint32_t)p[0] | (uint32_t)p[1] << 8 |
           (uint32_t)p[2] << 16 | (uint32_t)p[3] << 24;
}

static int load_picture(const char *path, struct picture *picture)
{
    unsigned char header[16];
    FILE *file = fopen(path, "rb");
    if (!file) return -1;
    int ok = fread(header, 1, sizeof(header), file) == sizeof(header) &&
             memcmp(header, "BSPLASH1", 8) == 0;
    if (ok) {
        picture->width = little32(header + 8);
        picture->height = little32(header + 12);
        ok = picture->width && picture->width <= 1920 &&
             picture->height && picture->height <= 1080;
    }
    if (ok) {
        size_t size = (size_t)picture->width * picture->height * 3;
        picture->rgb = malloc(size);
        ok = picture->rgb && fread(picture->rgb, 1, size, file) == size && fgetc(file) == EOF;
    }
    fclose(file);
    if (!ok) { free(picture->rgb); picture->rgb = NULL; return -1; }
    return 0;
}

static int valid_channel(struct fb_bitfield field, unsigned int bpp)
{
    return field.length <= 8 && field.offset <= bpp &&
           field.length + field.offset <= bpp && !field.msb_right;
}

static uint32_t channel(unsigned int value, struct fb_bitfield field)
{
    if (!field.length) return 0;
    return ((value * ((1u << field.length) - 1u) + 127u) / 255u) << field.offset;
}

static int paint(unsigned char *memory, size_t capacity,
                 const struct fb_fix_screeninfo *fixed,
                 const struct fb_var_screeninfo *var, const struct picture *picture)
{
    unsigned int bpp = var->bits_per_pixel;
    if (fixed->type != FB_TYPE_PACKED_PIXELS || fixed->visual != FB_VISUAL_TRUECOLOR ||
        (bpp != 16 && bpp != 24 && bpp != 32) ||
        !valid_channel(var->red, bpp) || !valid_channel(var->green, bpp) ||
        !valid_channel(var->blue, bpp) || !valid_channel(var->transp, bpp) ||
        !var->red.length || !var->green.length || !var->blue.length ||
        !var->xres || !var->yres || var->xres > 8192 || var->yres > 8192)
        return -1;
    unsigned int bytes = bpp / 8;
    uint64_t right = ((uint64_t)var->xoffset + var->xres) * bytes;
    uint64_t end = ((uint64_t)var->yoffset + var->yres - 1) * fixed->line_length + right;
    if (right > fixed->line_length || end > capacity) return -1;
    uint32_t width = picture->width < var->xres ? picture->width : var->xres;
    uint32_t height = picture->height < var->yres ? picture->height : var->yres;
    uint32_t left = (var->xres - width) / 2, top = (var->yres - height) / 2;
    uint32_t crop_x = (picture->width - width) / 2, crop_y = (picture->height - height) / 2;
    for (uint32_t y = 0; y < var->yres; ++y) {
        unsigned char *row = memory + (size_t)(var->yoffset + y) * fixed->line_length +
                             (size_t)var->xoffset * bytes;
        for (uint32_t x = 0; x < var->xres; ++x) {
            const unsigned char *rgb = picture->rgb; /* solid border from top-left pixel */
            if (x >= left && x < left + width && y >= top && y < top + height)
                rgb += ((size_t)(y - top + crop_y) * picture->width + x - left + crop_x) * 3;
            uint32_t pixel = channel(rgb[0], var->red) | channel(rgb[1], var->green) |
                             channel(rgb[2], var->blue) | channel(255, var->transp);
            for (unsigned int byte = 0; byte < bytes; ++byte)
                row[(size_t)x * bytes + byte] = (unsigned char)(pixel >> (byte * 8));
        }
    }
    return 0;
}

int main(int argc, char **argv)
{
    if (argc != 3) { fprintf(stderr, "usage: %s picture.bin /dev/fb0\n", argv[0]); return 2; }
    struct picture picture = {0};
    if (load_picture(argv[1], &picture)) { fprintf(stderr, "Invalid splash image\n"); return 1; }
    int result = 1, fd = open(argv[2], O_RDWR | O_CLOEXEC);
    struct fb_fix_screeninfo fixed;
    struct fb_var_screeninfo var;
    if (fd < 0 || ioctl(fd, FBIOGET_FSCREENINFO, &fixed) || ioctl(fd, FBIOGET_VSCREENINFO, &var))
        goto done;
    if (!fixed.smem_len || fixed.smem_len > 256u * 1024u * 1024u) goto done;
    void *memory = mmap(NULL, fixed.smem_len, PROT_READ | PROT_WRITE, MAP_SHARED, fd, 0);
    if (memory == MAP_FAILED) goto done;
    result = paint(memory, fixed.smem_len, &fixed, &var, &picture) ? 1 : 0;
    munmap(memory, fixed.smem_len);
    if (!result) printf("Bosun splash drawn on %ux%u framebuffer\n", var.xres, var.yres);
done:
    if (fd >= 0) close(fd);
    free(picture.rgb);
    return result;
}

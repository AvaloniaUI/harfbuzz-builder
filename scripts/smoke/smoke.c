/* Smoke test of a libnharfbuzz build: smoke <font> <expected version>
 *
 * Shapes a string, subsets the font, and draws a glyph with the raster, vector and GPU libraries. On Apple and
 * Windows it also creates a face with CoreText or DirectWrite. Exits with a non-zero status on the first failure. */

#include <stdio.h>
#include <string.h>

#include <hb.h>
#include <hb-features.h>
#include <hb-ot.h>
#include <hb-subset.h>
#include <hb-raster.h>
#include <hb-vector.h>
#include <hb-gpu.h>
#ifdef HB_HAS_CORETEXT
#include <hb-coretext.h>
#endif
#ifdef HB_HAS_DIRECTWRITE
#include <hb-directwrite.h>
#endif

#define CHECK(cond)                                                    \
  do {                                                                 \
    if (!(cond)) {                                                     \
      fprintf (stderr, "smoke: %s:%d: %s\n", __FILE__, __LINE__, #cond); \
      return 1;                                                        \
    }                                                                  \
  } while (0)

static const char TEXT[] = "Hello, HarfBuzz";

static int
shape (hb_font_t *font, hb_codepoint_t *first_glyph)
{
  hb_buffer_t *buffer = hb_buffer_create ();
  hb_buffer_add_utf8 (buffer, TEXT, -1, 0, -1);
  hb_buffer_guess_segment_properties (buffer);
  hb_shape (font, buffer, NULL, 0);

  unsigned int count = 0;
  hb_glyph_info_t *info = hb_buffer_get_glyph_infos (buffer, &count);
  hb_glyph_position_t *pos = hb_buffer_get_glyph_positions (buffer, NULL);
  CHECK (count == strlen (TEXT));
  hb_position_t advance = 0;
  for (unsigned int i = 0; i < count; i++)
  {
    CHECK (info[i].codepoint != 0);
    advance += pos[i].x_advance;
  }
  CHECK (advance > 0);
  *first_glyph = info[0].codepoint;
  printf ("shape: %u glyphs, advance %d\n", count, advance);
  hb_buffer_destroy (buffer);
  return 0;
}

static int
subset (hb_face_t *face)
{
  hb_subset_input_t *input = hb_subset_input_create_or_fail ();
  CHECK (input);
  hb_set_t *unicodes = hb_subset_input_unicode_set (input);
  for (const char *c = TEXT; *c; c++)
    hb_set_add (unicodes, (hb_codepoint_t) (unsigned char) *c);
  hb_face_t *result = hb_subset_or_fail (face, input);
  CHECK (result);
  unsigned int before = hb_face_get_glyph_count (face), after = hb_face_get_glyph_count (result);
  CHECK (after > 1 && after < before);
  printf ("subset: %u -> %u glyphs\n", before, after);
  hb_face_destroy (result);
  hb_subset_input_destroy (input);
  return 0;
}

static int
draw (hb_font_t *font, hb_codepoint_t glyph)
{
  hb_raster_draw_t *raster = hb_raster_draw_create_or_fail ();
  CHECK (raster);
  CHECK (hb_raster_draw_glyph_or_fail (raster, font, glyph));
  hb_raster_image_t *image = hb_raster_draw_render (raster);
  CHECK (image);
  hb_raster_extents_t extents;
  hb_raster_image_get_extents (image, &extents);
  CHECK (extents.width > 0 && extents.height > 0);
  printf ("raster: %ux%u\n", extents.width, extents.height);
  hb_raster_image_destroy (image);
  hb_raster_draw_destroy (raster);

  hb_vector_draw_t *vector = hb_vector_draw_create_or_fail (HB_VECTOR_FORMAT_SVG);
  CHECK (vector);
  CHECK (hb_vector_draw_glyph_or_fail (vector, font, glyph, HB_VECTOR_EXTENTS_MODE_EXPAND));
  hb_blob_t *svg = hb_vector_draw_render (vector);
  CHECK (svg && hb_blob_get_length (svg) > 0);
  printf ("vector: %u bytes of SVG\n", hb_blob_get_length (svg));
  hb_blob_destroy (svg);
  hb_vector_draw_destroy (vector);

  hb_gpu_draw_t *gpu = hb_gpu_draw_create_or_fail ();
  CHECK (gpu);
  CHECK (hb_gpu_draw_glyph_or_fail (gpu, font, glyph));
  hb_glyph_extents_t glyph_extents;
  hb_blob_t *encoded = hb_gpu_draw_encode (gpu, &glyph_extents);
  CHECK (encoded && hb_blob_get_length (encoded) > 0);
  printf ("gpu: %u bytes\n", hb_blob_get_length (encoded));
  hb_blob_destroy (encoded);
  hb_gpu_draw_destroy (gpu);
  return 0;
}

static int
platform (const char *path)
{
#ifdef HB_HAS_CORETEXT
  hb_face_t *face = hb_coretext_face_create_from_file_or_fail (path, 0);
  CHECK (face);
  CHECK (hb_face_get_glyph_count (face) > 0);
  printf ("coretext: %u glyphs\n", hb_face_get_glyph_count (face));
  hb_face_destroy (face);
#endif
#ifdef HB_HAS_DIRECTWRITE
  hb_face_t *face = hb_directwrite_face_create_from_file_or_fail (path, 0);
  CHECK (face);
  CHECK (hb_face_get_glyph_count (face) > 0);
  printf ("directwrite: %u glyphs\n", hb_face_get_glyph_count (face));
  hb_face_destroy (face);
#endif
  (void) path;
  return 0;
}

int
main (int argc, char **argv)
{
  if (argc != 3)
  {
    fprintf (stderr, "usage: smoke <font> <expected version>\n");
    return 2;
  }
  printf ("version: %s\n", hb_version_string ());
  CHECK (strcmp (hb_version_string (), argv[2]) == 0);
  CHECK (hb_version_atleast (HB_VERSION_MAJOR, HB_VERSION_MINOR, HB_VERSION_MICRO));

  hb_blob_t *blob = hb_blob_create_from_file_or_fail (argv[1]);
  CHECK (blob);
  hb_face_t *face = hb_face_create (blob, 0);
  CHECK (hb_face_get_glyph_count (face) > 0);
  hb_font_t *font = hb_font_create (face);

  hb_codepoint_t glyph = 0;
  if (shape (font, &glyph) || subset (face) || draw (font, glyph) || platform (argv[1]))
    return 1;

  hb_font_destroy (font);
  hb_face_destroy (face);
  hb_blob_destroy (blob);
  printf ("ok\n");
  return 0;
}

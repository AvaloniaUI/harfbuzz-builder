/* Force-included (/FI) by build.py in clang-cl builds when the Windows SDK's NAN is not a constant expression.
 *
 * The UCRT of some Windows SDKs (10.0.22621) defines NAN as -(float)(INFINITY * 0.0F), which clang rejects in
 * constant expressions such as HarfBuzz's "static constexpr float INVALID = NAN;". math.h has an include guard, so
 * the definition below stays in effect for the rest of the translation unit. */
#include <math.h>
#undef NAN
#define NAN __builtin_nanf ("")

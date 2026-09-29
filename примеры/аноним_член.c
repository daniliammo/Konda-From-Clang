/* Безымянные члены записей (C11): «union { float el[4]; struct { float x, y, z, w; }; };»
 * внутри структуры (как weston_vec4f/weston_mat4f). В Konda у члена и у типа
 * должно быть имя: kfc поднимает их как «Внешняя_анонN» (союз/структура) с
 * членом «_анонN»; доступ «v.x» → «v._анон0._анон0.x» (член союза — под
 * «небезопасно», как любой type punning). */
#include <stdio.h>
struct v4 { union { float el[4]; struct { float x, y, z, w; }; }; };
struct м4 { union { struct v4 col[4]; float d[16]; }; };
static float f(struct v4 *v) { return v->x + v->el[1] + v->w; }
int main(void)
{
	struct v4 в;
	в.el[0] = 1; в.el[1] = 2; в.el[2] = 3; в.el[3] = 4;
	struct м4 м;
	м.d[5] = 7;
	printf("%.1f %.1f\n", f(&в), м.col[1].el[1]);
	return 0;
}

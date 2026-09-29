/* «sizeof(выражение)» → «размер_обьекта(ТИП)»: Konda принимает только тип,
 * kfc берёт тип операнда у clang; массив — «размер_обьекта(T) * N». */
#include <stdio.h>
#include <stdlib.h>

struct точка { int x, y; };

int main(void)
{
    char заголовок[16];
    int ряд[5];
    struct точка т = { 1, 2 };
    struct точка *п = malloc(sizeof(*п));
    п->x = т.x + т.y;
    snprintf(заголовок, sizeof(заголовок), "п=%d", п->x);
    printf("%s %d %d %d %d\n", заголовок, (int)sizeof(ряд), (int)sizeof т,
           (int)sizeof(ряд[0]), (int)sizeof(*п));
    free(п);
    return 0;
}

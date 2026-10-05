// Присваивание и запятая внутри выражения: «if ((x = f()) == NULL)»,
// «y = a - (h = 3)», «while (pid = next(), pid > 0)» — kfc выносит
// присваивание строкой перед оператором (в Konda оно — только оператор).
#include <stdio.h>
#include <stdlib.h>
static int k = 0;
static int next(void) { return k < 3 ? ++k : -1; }
int main(void) {
    int h, y, pid, s = 0;
    char *loc;
    if ((loc = getenv("НЕТ_ТАКОЙ_ПЕРЕМЕННОЙ")) == NULL)
        s += 100;
    y = 50 - (h = 10 * 3);
    while (pid = next(), pid > 0)
        s += pid;
    printf("%d %d %d\n", s, y, h);
    return 0;
}

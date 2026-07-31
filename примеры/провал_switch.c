/* Регрессия: ложный «switch-провал». (1) «default: case X:» — состекованная с
 * default метка (clang вкладывает её как substatement) не должна утекать в
 * операторы. (2) «case: …; exit()» — диверджентный вызов завершает поток, это
 * не провал. Оба раньше ложно помечались. */
#include <stdio.h>
#include <stdlib.h>

static int выбрать(int x)
{
    int r = 0;
    switch (x) {
    default:
    case 1:
    case 2:
        r = 10;
        break;
    case 3:
        r = 20;
        break;
    case 9:
        exit(3);                 /* расходится — провала нет */
    }
    return r;
}

int main(void)
{
    printf("r=%d\n", выбрать(3));
    return 0;
}

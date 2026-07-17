#include <stdio.h>
#include <stdlib.h>

struct Точка { int x; int y; };

enum Цвет { КРАСНЫЙ, ЗЕЛЁНЫЙ = 5, СИНИЙ };

int площадь(struct Точка т) {
    return т.x * т.y;
}

int классифицировать(int n) {
    switch (n) {
        case 0:
            return 100;
        case 1:
        case 2:
            return 200;
        default:
            return 300;
    }
}

int main(void) {
    struct Точка т = { 3, 4 };
    printf("площадь=%d\n", площадь(т));

    enum Цвет ц = ЗЕЛЁНЫЙ;
    printf("цвет=%d\n", ц);

    int *данные = malloc(4 * sizeof(int));
    for (int i = 0; i < 4; i++)
        данные[i] = i * i;
    printf("данные[2]=%d\n", данные[2]);
    free(данные);

    int знак = (площадь(т) > 10) ? 1 : -1;
    printf("знак=%d\n", знак);

    printf("класс=%d\n", классифицировать(2));
    return 0;
}

/* Идиома «присваивание встроено в условие цикла» — while/do-while/for.
 * В Konda присваивание — оператор, не выражение, поэтому конвертер выносит
 * присваивание в тело (пока (истина) { x = f(); если (усл) {…} иначе {прервать} }).
 * Проверяем, что все три формы переводятся и дают тот же результат, что C. */
#include <stdio.h>

int next(int x);
int next(int x) { return x + 1; }

int main(void) {
    int v;

    /* while: (v = next(i)) < 5  → сумма 1+2+3+4 = 10 */
    int i = 0, sw = 0;
    while ((v = next(i)) < 5) {
        sw = sw + v;
        i = v;
    }
    printf("while=%d\n", sw);

    /* do-while: условие с присваиванием в конце итерации */
    int j = 0, sd = 0;
    do {
        sd = sd + j;
        j = j + 1;
    } while ((v = next(j)) < 4);
    printf("do=%d\n", sd);

    /* for: присваивание в условии, без continue → переписывается в «пока» */
    int k, sf = 0;
    for (k = 0; (v = next(k)) < 3; k = v) {
        sf = sf + v;
    }
    printf("for=%d\n", sf);

    return 0;
}

/* Растущий буфер: указатель, который РЕАЛЛОЦИРУЕТСЯ, → «буфер<T>» (§59
   транспилятора); malloc→выделить, realloc→перевыделить, free снимается
   (autofree). Индексация — с проверкой границ, как у среза. */
#include <stdlib.h>
#include <stdio.h>

int main(void) {
    int *p = malloc(2 * sizeof(int));
    p[0] = 10;
    p[1] = 20;
    p = realloc(p, 4 * sizeof(int));   /* → перевыделить(p, 4): длина=4 */
    p[2] = 30;
    p[3] = 40;
    int s = p[0] + p[1] + p[2] + p[3];
    free(p);                            /* снимается — autofree буфера */
    printf("%d\n", s);
    return 0;
}

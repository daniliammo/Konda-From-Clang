/* Конвенция освобождения по ИМЕНИ: «*_free»/«*_destroy» на владеющем срезе →
   снять (autofree) + пометка ПРОВЕРИТЬ (деструктор мог делать больше, чем free).
   Чистый free — снять молча. Знания владения нет в типах C — берём из имени. */
#include <stdlib.h>
#include <stdio.h>

static void массив_free(int *p) { free(p); }

int main(void) {
    int *a = malloc(3 * sizeof(int));
    a[0] = 10; a[1] = 20; a[2] = 30;
    int s = a[0] + a[1] + a[2];
    массив_free(a);          /* → снят (autofree) + ПРОВЕРИТЬ */
    printf("%d\n", s);
    return 0;
}

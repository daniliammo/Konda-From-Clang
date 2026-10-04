// Указатель-алиас на элемент/поле «struct buf *b = &d->bufs[i]»: kfc
// подставляет само lvalue вместо «b» (без «&»), вызов «mark(b)» — через
// «изменяемый» (ссылка на проекцию). Индекс динамический — проверка границ.
#include <stdio.h>
struct buf { int busy; int w; };
struct disp { struct buf bufs[4]; int n; };
static void mark(struct buf *b) { b->busy = 2; }
static int f(struct disp *d, int i) {
    struct buf *b = &d->bufs[i];
    b->busy = 1;
    b->w = 10 + i;
    mark(b);
    return b->busy + (*b).w;
}
int main(void) {
    struct disp d = {0};
    int s = f(&d, 1) + f(&d, 3);
    printf("алиас=%d %d\n", s, d.bufs[3].busy);
    return 0;
}

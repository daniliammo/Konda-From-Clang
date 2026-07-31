#include <stdio.h>

struct point { int x; int y; };

static void inc(int *v) { *v += 1; }
static void set42(int *v) { *v = 42; }

int main(void)
{
    struct point p = {1, 2};
    inc(&p.x);
    int arr[3] = {7, 8, 9};
    set42(&arr[1]);
    struct point *pp = &p;
    inc(&pp->y);
    printf("%d %d %d\n", p.x, arr[1], p.y);
    return 0;
}

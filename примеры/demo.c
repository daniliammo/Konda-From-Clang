#include <stdio.h>

int сумма(int a, int b) { return a + b; }

int main(void) {
    int x = сумма(2, 3);
    for (int i = 0; i < x; i++)
        printf("%d\n", i);
    int n = 0;
    while (n < 3) {
        printf("n=%d\n", n);
        n += 1;
    }
    if (x > 4) {
        printf("больше\n");
    } else {
        printf("меньше\n");
    }
    return 0;
}

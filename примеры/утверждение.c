/* C «assert(усл)» → встроенная Konda «утверждать(усл)» (§120 языка):
 * текст условия, строка и функция в сообщении; указатель → «p != нуль». */
#include <assert.h>
#include <stdio.h>

static int сумма(const int *a, int n)
{
    assert(n > 0 && n <= 4);
    int s = 0;
    for (int i = 0; i < n; i++)
        s += a[i];
    return s;
}

int main(void)
{
    int a[4] = {1, 2, 3, 4};
    const char *имя = "konda";
    assert(имя);
    assert(сумма(a, 4) == 10);
    printf("%d\n", сумма(a, 3));
    return 0;
}

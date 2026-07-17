#include <stdio.h>

union Значение { int ц; float в; };

int сумма_массива(int *a, int n) {
    int s = 0;
    for (int i = 0; i < n; i++)
        s = s + a[i];
    return s;
}

int первый_символ(char *с) {
    char *д = с + 1;
    return д[0];
}

int main(void) {
    int м[3] = { 10, 20, 30 };
    printf("сумма=%d\n", сумма_массива(м, 3));

    char *текст = "ABCD";
    printf("символ=%c\n", (char)первый_символ(текст));

    union Значение з;
    з.ц = 65;
    printf("union=%d\n", з.ц);

    char *байты = "WXYZ";
    int *как_инт = (int *)байты;
    printf("reinterpret=%d\n", как_инт[0] & 0xFF);
    return 0;
}

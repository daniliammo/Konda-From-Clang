/* C «__func__»/«__FUNCTION__» → встроенная Konda «имя_функции()» (§118
 * транспилятора). В main имя сохраняется литералом "main": Konda-имя точки
 * входа другое («точка_входа»), а вывод должен совпасть с C. */
#include <stdio.h>

static int сложить(int a, int b)
{
    const char *имя = __func__;
    printf("%s:%s:%d ", имя, __FUNCTION__, a + b);
    return a + b;
}

int main(void)
{
    int итог = сложить(1, 2);
    printf("%s:%d\n", __func__, итог);
    return 0;
}

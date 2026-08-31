/* Nullability-атрибуты clang как ПРЯМОЕ свидетельство владения из исходника
   (§ «конвенции/аннотации»): «_Nullable» → «возможно<T*>» (и НЕ становится
   ссылкой-изменяемый), «_Nonnull» → ненулевой (может быть ссылкой). */
#include <stdio.h>

static int первый_или(int * _Nullable p, int умолч) {
    if (p != 0) { return *p; }   /* p — возможно<целое32*>, дереф под охранником */
    return умолч;
}

static int удвой(int * _Nonnull q) {  /* q ненулевой → ссылка изменяемый */
    return *q + *q;
}

int main(void) {
    int v = 21;
    printf("%d %d\n", первый_или(0, 77), удвой(&v));
    return 0;
}

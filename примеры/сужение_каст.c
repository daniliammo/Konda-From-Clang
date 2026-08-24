/* size_t→int сужение (ОЧЕНЬ частое): «int len = strlen(s)», «int n = sizeof*4».
 * strlen/sizeof возвращают size_t (целое64); присваивание в целое32 — сужение.
 * Конвертер «развернуть» снимал clang-каст (IntegralCast) → транспилятор ругался
 * «неявное сужение… как<>()», а цикл проверки лишне оборачивал тело в
 * «небезопасно». Теперь конвертер эмитит «как<целое32>(…)». Результат = C. */
#include <stdio.h>
#include <string.h>

int main(void) {
    const char *msg = "hello";
    int len = strlen(msg);          // size_t → как<целое32>
    int n = sizeof(int) * 4;        // size_t → как<целое32>
    int cmp = strcmp(msg, "hello");
    printf("%d %d %d\n", len, n, cmp);
    return 0;
}

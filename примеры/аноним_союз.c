// Анонимный union/struct внутри структуры: доступ «v.col[i].el[j]» проходит
// через НЕИМЕНОВАННЫЙ член (clang вставляет неявный MemberExpr с пустым именем).
// Регресс: kfc печатал лишнюю точку — «v..col[i]..el[j]» вместо «v.col[i].el[j]».
#include <stdio.h>

struct vec2 {
    union {
        float el[2];
        struct { float x, y; };
    };
};

struct mat2 {
    union {
        struct vec2 col[2];
        float raw[4];
    };
};

int main(void)
{
    struct mat2 m;
    for (int i = 0; i < 2; i++)
        for (int j = 0; j < 2; j++)
            m.col[i].el[j] = (float)(i * 2 + j);
    printf("%.0f %.0f %.0f %.0f\n",
           m.col[0].el[0], m.col[0].el[1], m.col[1].el[0], m.col[1].el[1]);
    return 0;
}

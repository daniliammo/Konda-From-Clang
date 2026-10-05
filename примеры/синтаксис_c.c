// Конструкции C, которые раньше давали СИНТАКСИЧЕСКИ невалидный .конда:
// анонимная запись как тип переменной, вложенный enum, typedef массива, VLA
// с const-размером, вызов «(*fp)(…)», унарный «+», «(void)f()», «_Generic»,
// GNU statement expression (container_of weston), union с полем-массивом,
// «for» с пустой инициализацией и с запятыми, typedef функционального типа и
// псевдоним скаляра (разворачиваются без пометок).
#include <stdio.h>
#include <stddef.h>

#define container_of(ptr, type, member) ({                         \
	const __typeof__(((type *)0)->member) *__mptr = (ptr);           \
	(type *)((char *)__mptr - offsetof(type, member)); })

typedef float vec3[3];
typedef int (*обработчик_т)(int);
typedef int функция_т(int);
typedef unsigned int счётчик_т;

struct узел { int вес; int связь; };

struct фигура {
	int сторон;
	enum вид { КРУГ = 1, КВАДРАТ } вид;
	enum { МАЛАЯ, БОЛЬШАЯ } размер;
};

union значение { float числа[4]; int целое; };

static int удвоить(int на) { return на * 2; }

/* GNU statement expression «container_of» → «контейнер_из» (не вызывается:
 * параметр нужен сырым указателем, проверяется форма перевода). */
int вес_по_связи(int *связь)
{
	struct узел *у = container_of(связь, struct узел, связь);
	return у->вес;
}

static int сумма_таблицы(void)
{
	static const int строк = 2, колонок = 3;
	static const struct { int флаг; int вес; } таблица[] = { { 1, 10 }, { 2, 20 } };
	int буфер[строк * колонок + 1];
	int s = 0, i;
	for (i = 0; i < 2; i++)
		s += таблица[i].вес;
	буфер[строк * колонок] = s;
	return буфер[6];
}

int main(void)
{
	struct фигура ф = { 4, КВАДРАТ, БОЛЬШАЯ };
	vec3 вершины[2] = { { 1, 2, 3 }, { 4, 5, 6 } };
	union значение з = { { 0.5f, 1.5f, 2.5f, 3.5f } };
	обработчик_т fp = удвоить;
	функция_т *ф2 = удвоить;
	счётчик_т счёт = 0;
	int j = 0, k;

	счёт += (*fp)(+5) + ф2(1);
	(void)сумма_таблицы();
	for (; j < 3; j++)
		счёт += j;
	for (k = 0, j = 10; k < 2; k++, j--)
		счёт += j;
	счёт += _Generic(счёт, int: 100, default: 0);
	printf("синтаксис=%d %d %u %.1f %.1f %d\n", ф.вид, ф.размер, счёт,
	       вершины[1][2], з.числа[3], сумма_таблицы());
	return 0;
}

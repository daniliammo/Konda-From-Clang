/* Поле с именем ТИПА («struct window { struct display *display; }», weston):
 * в C теги и члены — разные пространства имён, в Konda — одно → поле
 * переименовывается в «display_» (определение, доступ, инициализаторы). */
#include <stdio.h>
struct display { int номер; };
struct window { struct display *display; int ширина; };
static int номер_окна(struct window *w) { return w->display->номер + w->ширина; }
int main(void)
{
	struct display д = { 40 };
	struct window о = { &д, 2 };
	printf("%d\n", номер_окна(&о));
	return 0;
}

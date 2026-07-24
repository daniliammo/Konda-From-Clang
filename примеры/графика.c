/*
 * Простой пример OpenGL + GLUT (фиксированный конвейер)
 * Компиляция (Linux): gcc -o triangle triangle.c -lglut -lGLU -lGL
 * Компиляция (Windows, с MinGW): gcc -o triangle.exe triangle.c -lfreeglut -lopengl32 -lglu32
 */

#include <GL/glut.h>   // или <GL/freeglut.h> на некоторых системах

/* Функция отрисовки кадра */
void display(void)
{
    /* Очищаем буфер цвета (фон — чёрный) */
    glClear(GL_COLOR_BUFFER_BIT);

    /* Начинаем рисовать треугольник */
    glBegin(GL_TRIANGLES);
        /* Вершина 1: красная, координаты (-0.5, -0.5) */
        glColor3f(1.0f, 0.0f, 0.0f);
        glVertex2f(-0.5f, -0.5f);

        /* Вершина 2: зелёная, координаты (0.5, -0.5) */
        glColor3f(0.0f, 1.0f, 0.0f);
        glVertex2f(0.5f, -0.5f);

        /* Вершина 3: синяя, координаты (0.0, 0.5) */
        glColor3f(0.0f, 0.0f, 1.0f);
        glVertex2f(0.0f, 0.5f);
    glEnd();   /* Заканчиваем рисование */

    /* Принудительно отправляем команды на выполнение */
    glFlush();
}

/* Точка входа */
int main(int argc, char **argv)
{
    /* Инициализация GLUT */
    glutInit(&argc, argv);

    /* Создаём окно */
    glutCreateWindow("Простой треугольник OpenGL");

    /* Регистрируем функцию отрисовки */
    glutDisplayFunc(display);

    /* Запускаем главный цикл обработки событий */
    glutMainLoop();

    return 0;
}

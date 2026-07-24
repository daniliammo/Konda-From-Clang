// «Библиотека» с out-параметром и регистрацией колбэка.
#pragma once
typedef void (*Обработчик)(int);
static inline void настроить(int *пargc, char **argv) { (void)argv; *пargc += 1; }
static inline void задать(Обработчик ф) { ф(42); }

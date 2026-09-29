/* Мини-«wayland-util.h»: звено интрузивного списка и операции над ним
 * (библиотечные для примера — как wl_list_init/insert/remove). */
#ifndef СПИСОК_H
#define СПИСОК_H
#include <stddef.h>

struct звено { struct звено *prev; struct звено *next; };

#define контейнер(ptr, sample, member) \
	(__typeof__(sample))((char *)(ptr) - offsetof(__typeof__(*sample), member))
#define для_всех(pos, head, member) \
	for (pos = контейнер((head)->next, pos, member); \
	     &pos->member != (head); \
	     pos = контейнер(pos->member.next, pos, member))
#define для_всех_обратно(pos, head, member) \
	for (pos = контейнер((head)->prev, pos, member); \
	     &pos->member != (head); \
	     pos = контейнер(pos->member.prev, pos, member))
#define для_всех_безопасно(pos, tmp, head, member) \
	for (pos = контейнер((head)->next, pos, member), \
	     tmp = контейнер((pos)->member.next, tmp, member); \
	     &pos->member != (head); \
	     pos = tmp, tmp = контейнер(pos->member.next, tmp, member))

static inline void список_начать(struct звено *список)
{
	список->prev = список;
	список->next = список;
}

static inline void список_вставить(struct звено *список, struct звено *эл)
{
	эл->prev = список->prev;
	эл->next = список;
	список->prev->next = эл;
	список->prev = эл;
}

static inline void список_убрать(struct звено *эл)
{
	эл->prev->next = эл->next;
	эл->next->prev = эл->prev;
}
#endif

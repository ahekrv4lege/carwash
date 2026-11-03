"""Проверка полуоткрытых интервалов по границам занятости и вместимости."""
def interval_problem(windows, occupied, start, end):
    cuts = {start, end}
    for a, b, _ in windows:
        if a < end and start < b:
            cuts.update((max(a, start), min(b, end)))
    for a, b in occupied:
        if a < end and start < b:
            cuts.update((max(a, start), min(b, end)))
    points = sorted(cuts)
    for a, b in zip(points, points[1:]):
        capacities = [cap for begin, finish, cap in windows if begin <= a and b <= finish]
        if not capacities:
            return "Это время вне графика работы"
        if None in capacities:
            return "Не указана вместимость графика; уточните число постов у администратора"
        if len(set(capacities)) > 1:
            return "В графике противоречивая вместимость пересекающихся интервалов"
        if sum(begin < b and a < finish for begin, finish in occupied) >= capacities[0]:
            return "Интервал занят: нет свободного поста на всю длительность услуги"
    return None

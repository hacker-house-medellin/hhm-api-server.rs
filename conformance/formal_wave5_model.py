def overlaps(a_start, a_end, b_start, b_end):
    return a_start < b_end and b_start < a_end

def admissible(existing, proposed):
    start, end = proposed
    return start < end and all(not overlaps(s, e, start, end) for s, e in existing)

room_reservations = [(0,2),(4,6)]
assert admissible(room_reservations,(2,4))
assert not admissible(room_reservations,(1,3))
assert not admissible(room_reservations,(5,7))
assert not admissible(room_reservations,(3,3))
print('formal_wave5_model: ok')

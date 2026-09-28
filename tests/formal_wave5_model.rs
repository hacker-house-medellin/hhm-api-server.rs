type Interval = (i32, i32);

fn overlaps(a: Interval, b: Interval) -> bool {
    return a.0 < b.1 && b.0 < a.1;
}

fn admissible(existing: &[Interval], proposed: Interval) -> bool {
    if proposed.0 >= proposed.1 {
        return false;
    }

    return existing
        .iter()
        .copied()
        .all(|interval| !overlaps(interval, proposed));
}

fn occupancy_oracle(existing: &[Interval], proposed: Interval) -> bool {
    if proposed.0 >= proposed.1 {
        return false;
    }

    for slot in proposed.0..proposed.1 {
        let occupied = existing
            .iter()
            .copied()
            .any(|(start, end)| start <= slot && slot < end);

        if occupied {
            return false;
        }
    }

    return true;
}

#[test]
fn preserves_room_reservation_boundary_examples() {
    let existing = [(0, 2), (4, 6)];

    assert!(admissible(&existing, (2, 4)));
    assert!(!admissible(&existing, (1, 3)));
    assert!(!admissible(&existing, (5, 7)));
    assert!(!admissible(&existing, (3, 3)));
}

#[test]
fn bounded_model_matches_independent_occupancy_oracle() {
    let existing = [(0, 2), (4, 6)];

    for start in -1..=7 {
        for end in -1..=7 {
            let proposed = (start, end);
            assert_eq!(
                admissible(&existing, proposed),
                occupancy_oracle(&existing, proposed),
                "room reservation admission mismatch for proposed interval {proposed:?}"
            );
        }
    }
}

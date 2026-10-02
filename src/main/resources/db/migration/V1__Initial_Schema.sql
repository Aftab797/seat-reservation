CREATE TABLE shows (
    id UUID PRIMARY KEY,
    name TEXT NOT NULL,
    price_paise BIGINT NOT NULL CHECK (price_paise >= 0),
    per_user_limit INTEGER NOT NULL CHECK (per_user_limit > 0),
    total_seats INTEGER NOT NULL CHECK (total_seats > 0),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE seats (
    id UUID PRIMARY KEY,
    show_id UUID NOT NULL REFERENCES shows(id),
    seat_number TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('AVAILABLE', 'CONFIRMED')),
    user_id TEXT,
    reservation_id UUID,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (show_id, seat_number)
);

CREATE INDEX idx_seats_show_status ON seats(show_id, status);
CREATE INDEX idx_seats_reservation ON seats(reservation_id);

CREATE TABLE reservations (
    id UUID PRIMARY KEY,
    show_id UUID NOT NULL REFERENCES shows(id),
    user_id TEXT NOT NULL,
    amount_paise BIGINT NOT NULL CHECK (amount_paise >= 0),
    status TEXT NOT NULL CHECK (status IN ('CONFIRMED', 'CANCELLED')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    cancelled_at TIMESTAMPTZ
);

CREATE TABLE idempotency_keys (
    show_id UUID NOT NULL REFERENCES shows(id),
    user_id TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    reservation_id UUID,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (show_id, user_id, idempotency_key)
);

CREATE TABLE user_show_quotas (
    show_id UUID NOT NULL REFERENCES shows(id),
    user_id TEXT NOT NULL,
    seats_held INTEGER NOT NULL CHECK (seats_held >= 0),
    PRIMARY KEY (show_id, user_id)
);


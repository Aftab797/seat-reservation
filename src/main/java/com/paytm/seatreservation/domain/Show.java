package com.paytm.seatreservation.domain;

import java.time.OffsetDateTime;
import java.util.UUID;

public record Show(
    UUID id,
    String name,
    Long pricePaise,
    Integer perUserLimit,
    Integer totalSeats,
    OffsetDateTime createdAt
) {}


package com.paytm.seatreservation.dto;

import java.util.List;
import java.util.UUID;

public record ShowResponse(
    UUID id,
    String name,
    Long price_paise,
    Integer total_seats,
    Integer available,
    Integer confirmed,
    List<SeatDto> seats
) {
    public record SeatDto(String seat, String status) {}
}


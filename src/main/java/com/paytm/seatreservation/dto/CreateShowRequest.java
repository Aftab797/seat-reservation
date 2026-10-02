package com.paytm.seatreservation.dto;

import java.util.List;

public record CreateShowRequest(
    String name,
    List<String> seats,
    Long price_paise,
    Integer per_user_limit
) {}


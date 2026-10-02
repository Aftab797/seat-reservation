package com.paytm.seatreservation.dto;

import java.util.List;

public record ReserveSeatsRequest(
    List<String> seats
) {}


package com.paytm.seatreservation.controller;

import com.paytm.seatreservation.dto.CreateShowRequest;
import com.paytm.seatreservation.dto.ShowResponse;
import com.paytm.seatreservation.service.ShowService;
import org.springframework.http.HttpStatus;
import org.springframework.web.bind.annotation.*;

import java.util.UUID;

@RestController
@RequestMapping("/shows")
public class ShowController {

    private final ShowService showService;
    private final com.paytm.seatreservation.service.ReservationService reservationService;

    public ShowController(ShowService showService, com.paytm.seatreservation.service.ReservationService reservationService) {
        this.showService = showService;
        this.reservationService = reservationService;
    }

    @PostMapping
    @ResponseStatus(HttpStatus.CREATED)
    public ShowResponse createShow(@RequestBody CreateShowRequest request) {
        return showService.createShow(request);
    }

    @PostMapping("/{id}/reserve")
    @ResponseStatus(HttpStatus.CREATED)
    public com.paytm.seatreservation.dto.ReservationResponse reserveSeats(
        @PathVariable UUID id,
        @RequestHeader(value = "Authorization") String authHeader,
        @RequestHeader(value = "Idempotency-Key", required = false) String idempotencyKey,
        @RequestBody com.paytm.seatreservation.dto.ReserveSeatsRequest request
    ) {
        String userId = authHeader.replace("Bearer ", "").trim();
        return reservationService.reserveSeats(id, userId, idempotencyKey != null ? idempotencyKey : UUID.randomUUID().toString(), request);
    }


    @PostMapping("/reservations/{reservationId}/cancel")
    public void cancelReservation(
        @PathVariable UUID reservationId,
        @RequestHeader(value = "Authorization") String authHeader
    ) {
        String userId = authHeader.replace("Bearer ", "").trim();
        reservationService.cancelReservation(reservationId, userId);
    }
    @GetMapping("/{id}")
    public ShowResponse getShow(@PathVariable UUID id) {
        return showService.getShow(id);
    }
}


package com.paytm.seatreservation.exception;

public class SeatTakenException extends RuntimeException {
    public SeatTakenException(String message) {
        super(message);
    }
}


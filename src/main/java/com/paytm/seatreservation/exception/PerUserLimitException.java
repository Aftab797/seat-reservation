package com.paytm.seatreservation.exception;

public class PerUserLimitException extends RuntimeException {
    public PerUserLimitException(String message) {
        super(message);
    }
}


package com.paytm.seatreservation.repository;

import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.stereotype.Repository;

import java.util.List;
import java.util.UUID;

@Repository
public class SeatRepository {

    private final JdbcTemplate jdbcTemplate;

    public SeatRepository(JdbcTemplate jdbcTemplate) {
        this.jdbcTemplate = jdbcTemplate;
    }

    public void batchInsertAvailableSeats(UUID showId, List<String> seatNumbers) {
        jdbcTemplate.batchUpdate(
            "INSERT INTO seats (id, show_id, seat_number, status) VALUES (?, ?, ?, 'AVAILABLE')",
            seatNumbers,
            100, // batch size
            (ps, seatNumber) -> {
                ps.setObject(1, UUID.randomUUID());
                ps.setObject(2, showId);
                ps.setString(3, seatNumber);
            }
        );
    }
}


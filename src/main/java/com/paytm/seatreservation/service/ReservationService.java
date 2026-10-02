package com.paytm.seatreservation.service;

import com.paytm.seatreservation.domain.Show;
import com.paytm.seatreservation.dto.ReservationResponse;
import com.paytm.seatreservation.dto.ReserveSeatsRequest;
import com.paytm.seatreservation.exception.InvalidRequestException;
import com.paytm.seatreservation.exception.PerUserLimitException;
import com.paytm.seatreservation.exception.ResourceNotFoundException;
import com.paytm.seatreservation.exception.SeatTakenException;
import com.paytm.seatreservation.repository.ShowRepository;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

import java.util.Collections;
import java.util.List;
import java.util.UUID;
import java.util.stream.Collectors;

@Service
public class ReservationService {
    private final io.micrometer.core.instrument.MeterRegistry meterRegistry;

    private final JdbcTemplate jdbcTemplate;
    private final ShowRepository showRepository;

    public ReservationService(JdbcTemplate jdbcTemplate, ShowRepository showRepository, io.micrometer.core.instrument.MeterRegistry meterRegistry) {
        this.jdbcTemplate = jdbcTemplate;
        this.showRepository = showRepository;
        this.meterRegistry = meterRegistry;
    }

    @Transactional
    public ReservationResponse reserveSeats(UUID showId, String userId, String idempotencyKey, ReserveSeatsRequest request) {
        long startTime = System.currentTimeMillis();
        try {
            if (request.seats() == null || request.seats().isEmpty()) {
                throw new InvalidRequestException("No seats requested");
            }

            List<String> sortedSeats = request.seats().stream()
                .distinct()
                .sorted()
                .collect(Collectors.toList());

            if (sortedSeats.size() != request.seats().size()) {
                throw new InvalidRequestException("Duplicate seats in request");
            }

            String requestHash = generateRequestHash(showId, sortedSeats);
            UUID reservationIdFromIdempotency = handleIdempotency(showId, userId, idempotencyKey, requestHash);

            if (reservationIdFromIdempotency != null) {
                 return fetchExistingReservation(reservationIdFromIdempotency, showId, userId);
            }

            Show show = showRepository.findById(showId)
                .orElseThrow(() -> new ResourceNotFoundException("Show not found"));

            // 1. Advisory Lock (user_id, show_id) to serialize this user's requests for this show
            long lockKey = generateLockKey(userId, showId);
            jdbcTemplate.execute("SELECT pg_advisory_xact_lock(" + lockKey + ")");

            // 2. Check and Increment Quota atomically
            int updatedQuota = incrementUserQuota(showId, userId, sortedSeats.size(), show.perUserLimit());
            if (updatedQuota == 0) {
                meterRegistry.counter("reservations_declined_total", "reason", "per-user-limit").increment();
                throw new PerUserLimitException("Per-user limit exceeded");
            }

            // 3. Lock Requested Seats explicitly to avoid deadlocks (ORDER BY seat_number)
            List<String> lockedSeats = lockAvailableSeats(showId, sortedSeats);

            if (lockedSeats.size() != sortedSeats.size()) {
                meterRegistry.counter("reservations_declined_total", "reason", "seat-taken").increment();
                throw new SeatTakenException("One or more requested seats are no longer available");
            }

            // 4. Create Reservation Record
            UUID reservationId = UUID.randomUUID();
            long amountPaise = show.pricePaise() * sortedSeats.size();
            jdbcTemplate.update(
                "INSERT INTO reservations (id, show_id, user_id, amount_paise, status) VALUES (?, ?, ?, ?, 'CONFIRMED')",
                reservationId, showId, userId, amountPaise
            );

            // 6. Update Idempotency Record
            jdbcTemplate.update(
                "UPDATE idempotency_keys SET reservation_id = ? WHERE show_id = ? AND user_id = ? AND idempotency_key = ?",
                reservationId, showId, userId, idempotencyKey
            );

            // 5. Update Seats conditionally
            int updatedSeats = jdbcTemplate.update(
                "UPDATE seats SET status = 'CONFIRMED', reservation_id = ?, user_id = ?, updated_at = now() " +
                "WHERE show_id = ? AND seat_number = ANY(?) AND status = 'AVAILABLE'",
                reservationId, userId, showId, sortedSeats.toArray(new String[0])
            );

            if (updatedSeats != sortedSeats.size()) {
                meterRegistry.counter("reservations_declined_total", "reason", "seat-taken").increment();
                throw new SeatTakenException("Concurrency conflict: seat was taken");
            }

            meterRegistry.counter("reservations_confirmed_total").increment();
            return new ReservationResponse(
                reservationId, showId, userId, sortedSeats, amountPaise, "CONFIRMED"
            );
        } finally {
            meterRegistry.timer("reservation_duration_seconds").record(java.time.Duration.ofMillis(System.currentTimeMillis() - startTime));
        }
    }

    private int incrementUserQuota(UUID showId, String userId, int requestedSeats, int limit) {
        return jdbcTemplate.update(
            "INSERT INTO user_show_quotas (show_id, user_id, seats_held) VALUES (?, ?, ?) " +
            "ON CONFLICT (show_id, user_id) DO UPDATE SET " +
            "seats_held = user_show_quotas.seats_held + EXCLUDED.seats_held " +
            "WHERE user_show_quotas.seats_held + EXCLUDED.seats_held <= ?",
            showId, userId, requestedSeats, limit
        );
    }

    private List<String> lockAvailableSeats(UUID showId, List<String> seatNumbers) {
        String inSql = String.join(",", Collections.nCopies(seatNumbers.size(), "?"));
        String sql = "SELECT seat_number FROM seats WHERE show_id = ? AND seat_number IN (" + inSql + ") " +
                     "AND status = 'AVAILABLE' ORDER BY seat_number FOR UPDATE SKIP LOCKED";

        Object[] params = new Object[seatNumbers.size() + 1];
        params[0] = showId;
        for (int i = 0; i < seatNumbers.size(); i++) {
            params[i + 1] = seatNumbers.get(i);
        }

        return jdbcTemplate.query(sql, (rs, rowNum) -> rs.getString("seat_number"), params);
    }

    private long generateLockKey(String userId, UUID showId) {
        return (userId + showId.toString()).hashCode();
    }

    private String generateRequestHash(UUID showId, List<String> sortedSeats) {
        String data = showId.toString() + "|" + String.join(",", sortedSeats);
        try {
            java.security.MessageDigest digest = java.security.MessageDigest.getInstance("SHA-256");
            byte[] hash = digest.digest(data.getBytes(java.nio.charset.StandardCharsets.UTF_8));
            StringBuilder hexString = new StringBuilder(2 * hash.length);
            for (byte b : hash) {
                String hex = Integer.toHexString(0xff & b);
                if (hex.length() == 1) {
                    hexString.append('0');
                }
                hexString.append(hex);
            }
            return hexString.toString();
        } catch (java.security.NoSuchAlgorithmException e) {
            throw new RuntimeException("SHA-256 algorithm not available", e);
        }
    }

    private UUID handleIdempotency(UUID showId, String userId, String idempotencyKey, String requestHash) {
        // Try to insert the idempotency record. If it exists, DO NOTHING.
        int inserted = jdbcTemplate.update(
            "INSERT INTO idempotency_keys (show_id, user_id, idempotency_key, request_hash) VALUES (?, ?, ?, ?) " +
            "ON CONFLICT (show_id, user_id, idempotency_key) DO NOTHING",
            showId, userId, idempotencyKey, requestHash
        );

        if (inserted == 1) {
            return null; // It's a new request
        }

        // Key exists. Fetch it to check for mismatch.
        List<java.util.Map<String, Object>> existing = jdbcTemplate.queryForList(
            "SELECT request_hash, reservation_id FROM idempotency_keys WHERE show_id = ? AND user_id = ? AND idempotency_key = ?",
            showId, userId, idempotencyKey
        );

        if (existing.isEmpty()) {
            // Unlikely race condition, but safe to return null to retry insert
            return null;
        }

        String existingHash = (String) existing.get(0).get("request_hash");
        UUID existingReservationId = (UUID) existing.get(0).get("reservation_id");

        if (!requestHash.equals(existingHash)) {
            meterRegistry.counter("reservations_declined_total", "reason", "idempotency_conflict").increment();
            throw new com.paytm.seatreservation.exception.IdempotencyConflictException("Idempotency key reused with different request body");
        }

        if (existingReservationId == null) {
            meterRegistry.counter("reservations_declined_total", "reason", "idempotency_conflict").increment();
             throw new com.paytm.seatreservation.exception.IdempotencyConflictException("Concurrent identical request in flight");
        }

        meterRegistry.counter("reservations_declined_total", "reason", "idempotent-replay").increment();
        return existingReservationId;
    }

    private ReservationResponse fetchExistingReservation(UUID reservationId, UUID showId, String userId) {
         List<java.util.Map<String, Object>> resData = jdbcTemplate.queryForList(
            "SELECT amount_paise, status FROM reservations WHERE id = ?",
            reservationId
        );

         List<String> seats = jdbcTemplate.queryForList(
            "SELECT seat_number FROM seats WHERE reservation_id = ? ORDER BY seat_number",
            String.class, reservationId
        );

        return new ReservationResponse(
            reservationId, showId, userId, seats, (Long) resData.get(0).get("amount_paise"), (String) resData.get(0).get("status")
        );
    }

    @Transactional
    public void cancelReservation(UUID reservationId, String userId) {
        // Find the reservation
        List<java.util.Map<String, Object>> resData = jdbcTemplate.queryForList(
            "SELECT show_id, user_id, status FROM reservations WHERE id = ?",
            reservationId
        );

        if (resData.isEmpty()) {
            throw new ResourceNotFoundException("Reservation not found");
        }

        UUID showId = (UUID) resData.get(0).get("show_id");
        String ownerId = (String) resData.get(0).get("user_id");
        String status = (String) resData.get(0).get("status");

        if (!userId.equals(ownerId)) {
            throw new com.paytm.seatreservation.exception.UnauthorizedException("Cannot cancel reservation belonging to another user");
        }

        if ("CANCELLED".equals(status)) {
            return; // Idempotent cancellation
        }

        // 1. Advisory Lock (user_id, show_id) to serialize this user's requests for this show
        long lockKey = generateLockKey(userId, showId);
        jdbcTemplate.execute("SELECT pg_advisory_xact_lock(" + lockKey + ")");

        // Find the seats currently confirmed for this reservation
        List<String> seats = jdbcTemplate.queryForList(
            "SELECT seat_number FROM seats WHERE reservation_id = ? ORDER BY seat_number FOR UPDATE",
            String.class, reservationId
        );

        if (seats.isEmpty()) {
            return; // Unlikely, but handle safely
        }

        // 2. Set reservation.status = CANCELLED
        jdbcTemplate.update(
            "UPDATE reservations SET status = 'CANCELLED', cancelled_at = now() WHERE id = ?",
            reservationId
        );

        // 3. Set those seats = AVAILABLE
        jdbcTemplate.update(
            "UPDATE seats SET status = 'AVAILABLE', reservation_id = NULL, user_id = NULL, updated_at = now() WHERE reservation_id = ?",
            reservationId
        );

        // 4. Decrement user_show_quotas by the number of released seats
        jdbcTemplate.update(
            "UPDATE user_show_quotas SET seats_held = seats_held - ? WHERE show_id = ? AND user_id = ?",
            seats.size(), showId, userId
        );
        meterRegistry.counter("reservations_cancelled_total").increment();
    }
}

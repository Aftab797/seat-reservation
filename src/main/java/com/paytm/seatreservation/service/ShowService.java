package com.paytm.seatreservation.service;

import com.paytm.seatreservation.domain.Show;
import com.paytm.seatreservation.dto.CreateShowRequest;
import com.paytm.seatreservation.dto.ShowResponse;
import com.paytm.seatreservation.exception.InvalidRequestException;
import com.paytm.seatreservation.exception.ResourceNotFoundException;
import com.paytm.seatreservation.repository.SeatRepository;
import com.paytm.seatreservation.repository.ShowRepository;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

import java.time.OffsetDateTime;
import java.util.List;
import java.util.UUID;

@Service
public class ShowService {

    private final ShowRepository showRepository;
    private final SeatRepository seatRepository;
    private final JdbcTemplate jdbcTemplate;

    public ShowService(ShowRepository showRepository, SeatRepository seatRepository, JdbcTemplate jdbcTemplate) {
        this.showRepository = showRepository;
        this.seatRepository = seatRepository;
        this.jdbcTemplate = jdbcTemplate;
    }

    @Transactional
    public ShowResponse createShow(CreateShowRequest request) {
        if (request.seats() == null || request.seats().isEmpty()) {
            throw new InvalidRequestException("Show must have at least one seat");
        }
        
        long distinctSeats = request.seats().stream().distinct().count();
        if (distinctSeats != request.seats().size()) {
            throw new InvalidRequestException("Duplicate seats provided");
        }

        UUID showId = UUID.randomUUID();
        Integer perUserLimit = request.per_user_limit() != null ? request.per_user_limit() : 4;
        
        Show show = new Show(
            showId,
            request.name(),
            request.price_paise(),
            perUserLimit,
            request.seats().size(),
            OffsetDateTime.now()
        );

        showRepository.insert(show);
        seatRepository.batchInsertAvailableSeats(showId, request.seats());

        return getShow(showId);
    }

    public ShowResponse getShow(UUID showId) {
        Show show = showRepository.findById(showId)
            .orElseThrow(() -> new ResourceNotFoundException("Show not found"));

        List<ShowResponse.SeatDto> seats = jdbcTemplate.query(
            "SELECT seat_number, status FROM seats WHERE show_id = ? ORDER BY seat_number",
            (rs, rowNum) -> new ShowResponse.SeatDto(
                rs.getString("seat_number"),
                rs.getString("status")
            ),
            showId
        );

        int available = 0;
        int confirmed = 0;
        for (ShowResponse.SeatDto seat : seats) {
            if ("AVAILABLE".equals(seat.status())) available++;
            else if ("CONFIRMED".equals(seat.status())) confirmed++;
        }

        return new ShowResponse(
            show.id(),
            show.name(),
            show.pricePaise(),
            show.totalSeats(),
            available,
            confirmed,
            seats
        );
    }
}


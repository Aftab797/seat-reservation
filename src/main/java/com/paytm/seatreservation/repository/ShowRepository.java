package com.paytm.seatreservation.repository;

import com.paytm.seatreservation.domain.Show;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.stereotype.Repository;

import java.sql.ResultSet;
import java.sql.SQLException;
import java.time.OffsetDateTime;
import java.util.List;
import java.util.Optional;
import java.util.UUID;

@Repository
public class ShowRepository {

    private final JdbcTemplate jdbcTemplate;

    public ShowRepository(JdbcTemplate jdbcTemplate) {
        this.jdbcTemplate = jdbcTemplate;
    }

    public void insert(Show show) {
        jdbcTemplate.update(
            "INSERT INTO shows (id, name, price_paise, per_user_limit, total_seats, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            show.id(), show.name(), show.pricePaise(), show.perUserLimit(), show.totalSeats(), show.createdAt()
        );
    }

    public Optional<Show> findById(UUID id) {
        List<Show> results = jdbcTemplate.query(
            "SELECT id, name, price_paise, per_user_limit, total_seats, created_at FROM shows WHERE id = ?",
            (rs, rowNum) -> mapRowToShow(rs),
            id
        );
        return results.isEmpty() ? Optional.empty() : Optional.of(results.get(0));
    }

    private Show mapRowToShow(ResultSet rs) throws SQLException {
        return new Show(
            rs.getObject("id", UUID.class),
            rs.getString("name"),
            rs.getLong("price_paise"),
            rs.getInt("per_user_limit"),
            rs.getInt("total_seats"),
            rs.getObject("created_at", OffsetDateTime.class)
        );
    }
}


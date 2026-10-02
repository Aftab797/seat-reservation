package com.paytm.seatreservation.controller;

import org.springframework.http.HttpStatus;
import org.springframework.http.ResponseEntity;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;

@RestController
@RequestMapping("/health")
public class HealthController {

    private final JdbcTemplate jdbcTemplate;

    public HealthController(JdbcTemplate jdbcTemplate) {
        this.jdbcTemplate = jdbcTemplate;
    }

    @GetMapping("/live")
    public ResponseEntity<String> liveness() {
        return ResponseEntity.ok("OK");
    }

    @GetMapping("/ready")
    public ResponseEntity<String> readiness() {
        try {
            jdbcTemplate.execute("SELECT 1");
            return ResponseEntity.ok("OK");
        } catch (Exception e) {
            return ResponseEntity.status(HttpStatus.SERVICE_UNAVAILABLE).body("Database unavailable");
        }
    }
}


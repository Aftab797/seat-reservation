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

    public ShowController(ShowService showService) {
        this.showService = showService;
    }

    @PostMapping
    @ResponseStatus(HttpStatus.CREATED)
    public ShowResponse createShow(@RequestBody CreateShowRequest request) {
        return showService.createShow(request);
    }

    @GetMapping("/{id}")
    public ShowResponse getShow(@PathVariable UUID id) {
        return showService.getShow(id);
    }
}


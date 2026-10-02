package com.paytm.seatreservation.config;

import jakarta.servlet.*;
import jakarta.servlet.http.HttpServletRequest;
import jakarta.servlet.http.HttpServletResponse;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.slf4j.MDC;
import org.springframework.core.Ordered;
import org.springframework.core.annotation.Order;
import org.springframework.stereotype.Component;

import java.io.IOException;
import java.util.UUID;

@Component
@Order(Ordered.HIGHEST_PRECEDENCE)
public class RequestLoggingFilter implements Filter {

    private static final Logger logger = LoggerFactory.getLogger(RequestLoggingFilter.class);
    private static final String REQUEST_ID_HEADER = "X-Request-ID";
    private static final String MDC_REQUEST_ID_KEY = "request_id";

    @Override
    public void doFilter(ServletRequest request, ServletResponse response, FilterChain chain)
            throws IOException, ServletException {
        if (request instanceof HttpServletRequest httpRequest && response instanceof HttpServletResponse httpResponse) {

            String requestId = httpRequest.getHeader(REQUEST_ID_HEADER);
            if (requestId == null || requestId.isEmpty()) {
                requestId = UUID.randomUUID().toString();
            }

            MDC.put(MDC_REQUEST_ID_KEY, requestId);

            long startTime = System.currentTimeMillis();

            try {
                chain.doFilter(request, response);
            } finally {
                long duration = System.currentTimeMillis() - startTime;
                logger.info(
                        "method=\"{}\" path=\"{}\" status={} duration_ms={} user_agent=\"{}\"",
                        httpRequest.getMethod(),
                        httpRequest.getRequestURI(),
                        httpResponse.getStatus(),
                        duration,
                        httpRequest.getHeader("User-Agent")
                );
                MDC.remove(MDC_REQUEST_ID_KEY);
            }
        } else {
            chain.doFilter(request, response);
        }
    }
}


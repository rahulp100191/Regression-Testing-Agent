FROM node:22-alpine AS build
WORKDIR /app
COPY frontend/package*.json ./
RUN npm ci
ARG NEXT_PUBLIC_API_BASE=http://127.0.0.1:8000
ENV NEXT_PUBLIC_API_BASE=$NEXT_PUBLIC_API_BASE
COPY frontend/ ./
RUN npm run build
FROM nginx:alpine
COPY --from=build /app/out /usr/share/nginx/html
EXPOSE 80

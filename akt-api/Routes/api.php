<?php

use Illuminate\Support\Facades\Route;

/**
 * Route::api() (app/Providers/Route.php) prefixes with config('api.prefix')/akt,
 * applies config('api.middleware') — the core `api` group: auth.basic.once +
 * permission:read-api + company.identify + read.only — and resolves the
 * namespace to Modules\AktApi\Http\Controllers\Api.
 *
 * Endpoint: GET /api/akt/ledgers
 */
Route::api('akt', function ($router) {
    $router->apiResource('ledgers', 'Ledgers')->only(['index']);
});

<?php

use Illuminate\Support\Facades\Route;

/**
 * Mirrors the DoubleEntry module's own API route registration — deliberately NOT
 * the Route::api() macro. That macro (a) wraps routes in a {company_id} URL
 * prefix (giving /{company}/api/... instead of the flat /api/... surface that
 * akt and every core API resource use) and (b) derives the controller namespace
 * from Str::studly(alias). A plain group with a fixed namespace avoids both:
 *
 *   Endpoint:  GET /api/akt-api/ledgers   (company is passed as ?company_id=)
 */
Route::group([
    'middleware' => 'api',
    'prefix' => 'api',
    'namespace' => 'Modules\AktApi\Http\Controllers\Api',
], function () {
    Route::group(['as' => 'api.'], function () {
        Route::get('akt-api/ledgers', 'Ledgers@index')->name('akt-api.ledgers.index');
        // Recode: repoint one item-leg posting to the correct GL account. This is
        // the in-place fix for transactions the module defaulted to 628/200/etc.
        Route::patch('akt-api/ledgers/{id}', 'Ledgers@update')->name('akt-api.ledgers.update');
        // Per-account debit/credit totals over an optional date window — powers
        // akt balance / trial-balance / report.
        Route::get('akt-api/balances', 'Balances@index')->name('akt-api.balances.index');
        // Orphaned ledger rows (ledgerable soft-deleted/missing) — report + prune.
        Route::get('akt-api/ledgers/orphans', 'Ledgers@orphans')->name('akt-api.ledgers.orphans');
        Route::delete('akt-api/ledgers/orphans', 'Ledgers@pruneOrphans')->name('akt-api.ledgers.prune-orphans');
    });
});

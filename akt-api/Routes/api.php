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
        // akt balance / trial-balance / report. Base-currency (per-leg converted).
        Route::get('akt-api/balances', 'Balances@index')->name('akt-api.balances.index');
        // Account type -> class map, read from the installation (kills the CLI's
        // hardcoded type_id->class table).
        Route::get('akt-api/account-types', 'Meta@accountTypes')->name('akt-api.account-types');
        // Convert an amount at a rate to the company default currency, using
        // Akaunting's own Currencies trait — mirrors convertToDefault for previews.
        Route::get('akt-api/convert', 'Convert@index')->name('akt-api.convert');
        // Orphaned ledger rows (ledgerable soft-deleted/missing) — report + prune.
        Route::get('akt-api/ledgers/orphans', 'Ledgers@orphans')->name('akt-api.ledgers.orphans');
        Route::delete('akt-api/ledgers/orphans', 'Ledgers@pruneOrphans')->name('akt-api.ledgers.prune-orphans');
        // Split: fan one item-leg posting out into N item legs (a multi-GL "split"
        // transaction, the way an invoice's line items post). The total (bank) leg
        // is untouched; the new legs must net to the original so the entry balances.
        Route::post('akt-api/ledgers/{id}/split', 'Ledgers@split')->name('akt-api.ledgers.split');
        // Banks with no DoubleEntry ledger mapping — an unmapped bank silently
        // posts nothing (powers `akt verify`). Read-only diagnostic.
        Route::get('akt-api/banks/unmapped', 'Banks@unmapped')->name('akt-api.banks.unmapped');
    });
});

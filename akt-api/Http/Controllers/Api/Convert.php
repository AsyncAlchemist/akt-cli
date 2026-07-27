<?php

namespace Modules\AktApi\Http\Controllers\Api;

use App\Abstracts\Http\ApiController;
use App\Traits\Currencies;
use Illuminate\Http\Request;

class Convert extends ApiController
{
    // Akaunting's own conversion logic — so the CLI mirrors it exactly instead of
    // recomputing base amounts locally.
    use Currencies;

    public function __construct()
    {
        $this->middleware('permission:read-double-entry-chart-of-accounts')->only('index');
    }

    /**
     * Base-currency value of an amount at a given historical rate, via Akaunting's
     * Currencies::convertToDefault (base = amount / currency_rate).
     *
     * Query: amount, currency_code, currency_rate.
     *
     * @return \Illuminate\Http\JsonResponse
     */
    public function index(Request $request)
    {
        $amount = (float) $request->input('amount', 0);
        $code   = (string) $request->input('currency_code', default_currency());
        $rate   = (float) $request->input('currency_rate', 1);

        $base = $this->convertToDefault($amount, $code, $rate ?: 1);

        return response()->json(['data' => [
            'base'             => (float) $base,
            'default_currency' => default_currency(),
        ]]);
    }
}

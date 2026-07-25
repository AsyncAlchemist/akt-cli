<?php

namespace Modules\AktApi\Http\Controllers\Api;

use App\Abstracts\Http\ApiController;
use Illuminate\Http\Request;
use Illuminate\Support\Facades\DB;
use Modules\AktApi\Http\Resources\Ledger as Resource;

class Ledgers extends ApiController
{
    /**
     * Gate on DoubleEntry read access — the data this exposes is DoubleEntry's,
     * and the admin/API role already holds this permission.
     *
     * We intentionally do NOT call parent::__construct(): the base
     * ApiController::__construct() runs assignPermissionsToController(), which
     * would auto-require a non-existent `read-akt-api-ledgers` permission and
     * return 403. Declaring our own middleware here (mirroring the DoubleEntry
     * module's own API controllers) is the supported pattern.
     */
    public function __construct()
    {
        $this->middleware('permission:read-double-entry-chart-of-accounts')->only('index');
    }

    /**
     * Display a listing of general-ledger postings.
     *
     * Reads DoubleEntry's ledger table directly (no DoubleEntry class imported).
     * The table name is singular: double_entry_ledger.
     *
     * @return \Illuminate\Http\JsonResponse
     */
    public function index(Request $request)
    {
        $query = DB::table('double_entry_ledger')
            ->where('company_id', company_id());

        foreach (['account_id', 'ledgerable_id'] as $intField) {
            if ($request->filled($intField)) {
                $query->where($intField, (int) $request->input($intField));
            }
        }

        foreach (['ledgerable_type', 'entry_type'] as $strField) {
            if ($request->filled($strField)) {
                $query->where($strField, $request->input($strField));
            }
        }

        if ($request->filled('issued_from')) {
            $query->where('issued_at', '>=', $request->input('issued_from'));
        }

        if ($request->filled('issued_to')) {
            $query->where('issued_at', '<=', $request->input('issued_to'));
        }

        $rows = $query->orderBy('issued_at')->orderBy('id')
            ->paginate((int) $request->input('limit', 100));

        return Resource::collection($rows);
    }
}

<?php

namespace Modules\AktApi\Http\Controllers\Api;

use App\Abstracts\Http\ApiController;
use Illuminate\Http\Request;
use Illuminate\Support\Facades\DB;
use Modules\DoubleEntry\Models\Ledger as DeLedger;
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
        $this->middleware('permission:read-double-entry-chart-of-accounts')->only('index', 'orphans');
        $this->middleware('permission:update-double-entry-chart-of-accounts')->only('update', 'pruneOrphans');
    }

    /**
     * Report orphaned ledger rows, grouped by ledgerable_type. An orphan is a
     * row the DoubleEntry Ledger model no longer returns (its ledgerable — the
     * transaction/document/journal — was soft-deleted or removed), so we diff the
     * raw table against the live ids the model yields.
     *
     * @return \Illuminate\Http\JsonResponse
     */
    public function orphans(Request $request)
    {
        $live = DeLedger::where('company_id', company_id())->pluck('id')->all();

        $by_type = DB::table('double_entry_ledger')
            ->where('company_id', company_id())
            ->when(! empty($live), fn ($q) => $q->whereNotIn('id', $live))
            ->groupBy('ledgerable_type')
            ->selectRaw('ledgerable_type, count(*) as c')
            ->pluck('c', 'ledgerable_type')
            ->all();

        return response()->json(['data' => ['total' => array_sum($by_type), 'by_type' => $by_type]]);
    }

    /**
     * Hard-delete orphaned ledger rows (see orphans()). These rows are already
     * dead — Akaunting's reports ignore them — so removing them is safe cleanup.
     *
     * @return \Illuminate\Http\JsonResponse
     */
    public function pruneOrphans(Request $request)
    {
        $live = DeLedger::where('company_id', company_id())->pluck('id')->all();

        $orphan_ids = DB::table('double_entry_ledger')
            ->where('company_id', company_id())
            ->when(! empty($live), fn ($q) => $q->whereNotIn('id', $live))
            ->pluck('id')
            ->all();

        $deleted = 0;
        foreach (array_chunk($orphan_ids, 500) as $batch) {
            $deleted += DB::table('double_entry_ledger')->whereIn('id', $batch)->delete();
        }

        return response()->json(['data' => ['deleted' => $deleted]]);
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

    /**
     * Repoint a single item-leg ledger posting to a different GL account.
     *
     * Only the `account_id` changes — debit/credit amounts and the paired
     * total-leg (bank) row are untouched, so the entry stays balanced. Restricted
     * to `entry_type = 'item'` rows so the bank leg can never be moved, and
     * company-scoped both on read and write.
     *
     * @param  int|string  $id
     * @return \Illuminate\Http\JsonResponse
     */
    public function update(Request $request, $id)
    {
        $request->validate(['account_id' => 'required|integer']);

        $row = DB::table('double_entry_ledger')
            ->where('id', (int) $id)
            ->where('company_id', company_id())
            ->first();

        if (! $row) {
            return $this->errorInternal('ledger row ' . $id . ' not found');
        }
        if ($row->entry_type !== 'item') {
            return $this->errorInternal("only item-leg rows may be recoded (row {$id} is '{$row->entry_type}')");
        }

        DB::table('double_entry_ledger')
            ->where('id', (int) $id)
            ->where('company_id', company_id())
            ->update([
                'account_id' => (int) $request->input('account_id'),
                'updated_at' => now(),
            ]);

        return new Resource((object) array_merge(
            (array) $row,
            ['account_id' => (int) $request->input('account_id')]
        ));
    }
}

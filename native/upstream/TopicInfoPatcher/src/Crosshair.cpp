// kalli/port-240: migrated to libxse/commonlibf4 7c8c6f8 for Fallout 4 1.11.240.
//
// Upstream tracked the crosshair actor through a ViewCasterUpdate event sink located with
// REL::VariantOffset(0x05A5F758, 0x02FF12F0, 0) (a namespace-scope relocation, i.e. computed at DLL load)
// and resolved handles with REL::VariantOffset(0x0000AB60, 0x0024C240, 0) (pre-NG, NG, VR). Neither has a
// 1.11.240 target (offsets240.h: both Unresolved; "use CommonLib handle API after review"), so the sink,
// the handle lookup and their relocations are omitted, not guessed. GetLastActorCoords keeps its Papyrus
// binding and the guard's documented disabled behaviour: an empty array (crosshair-hotkey start fails in
// Papyrus without a crash; use the activation perk or disable crosshair tracking).
#include "RE/Fallout.h"
#include "Guard240.h"  // kalli/port-240

#include <vector>

static_assert(mantella240::offset_known("ViewCasterUpdate event source") && !mantella240::offset_verified("ViewCasterUpdate event source"),
	"offsets240.h verified the ViewCasterUpdate source for 1.11.240: restore the crosshair sink with that reviewed target");
static_assert(mantella240::offset_known("BSPointerHandle lookup (crosshair)") && !mantella240::offset_verified("BSPointerHandle lookup (crosshair)"),
	"offsets240.h verified the crosshair handle lookup for 1.11.240: restore it with that reviewed target");

/*
* Unfortunately, returning Actor values sometimes doesn't work with Fallout4 NG (1.10.984.0)
* We instead return the X,Y,Z coordinates of the NPC and call Find.ClosestActor() in the Papyrus script
*/
std::vector<float> GetLastActorCoords(std::monostate) {
	// Fails closed: the guard never enables this native while its offsets are not Verified (tripwires above).
	if (!mantella240::plugin::allowed("GetLastActorCoords")) return {};
	REX::CRITICAL("GetLastActorCoords: enabled without a 1.11.240 crosshair source; refusing");  // unreachable
	return {};
}

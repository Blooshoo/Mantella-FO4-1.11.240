#include "RE/Fallout.h"
#include "Guard240.h"  // kalli/port-240

bool* LookupREFRByHandle(const uint32_t& handleIn, RE::NiPointer<RE::TESObjectREFR>& ref) {
	using func_t = decltype(LookupREFRByHandle);
	static REL::Relocation<func_t> func{ REL::VariantOffset(0x0000AB60, 0x0024C240, 0) };

	return func(handleIn, ref);
	}


struct ViewCasterUpdateEvent_MA {
	struct ViewCasterValueEvent_MA {
		uint32_t			crosshairHandler;	// 00	RefHandler
		uint32_t			unk04;				// 04
		uint32_t			unk08;				// 08
		uint32_t			unk0C;				// 0C
		uint64_t			unk10;				// 10
		uint64_t			unk18;				// 18
		uint32_t			unk20;				// 20
		uint32_t			unk24;				// 24
		};
	ViewCasterValueEvent_MA value;			// 00
	bool					unk28;			// 28
	uint32_t				unk2C;			// 2C
	bool					isNPC;			// 30 ?
	float					unk34;			// 34
	bool					canInteractive;	// 38
	};

typedef RE::BSTGlobalEvent::EventSource<ViewCasterUpdateEvent_MA> EventSource;
REL::Relocation<uint64_t*> g_Source(REL::VariantOffset(0x05A5F758, 0x02FF12F0, 0));

static uint32_t CrosshairRefHandle = 0;		// Zero if the crosshair is no longer on a reference
static uint32_t CrosshairActorHandle = 0;		// Memorize the last one until a new one is pointed at

static RE::Actor* lastActor;

class CrosshairChangeHandler_MA : public RE::BSTEventSink<ViewCasterUpdateEvent_MA> {
	public:
		virtual ~CrosshairChangeHandler_MA() {};

	virtual RE::BSEventNotifyControl ProcessEvent(const ViewCasterUpdateEvent_MA &evn, RE::BSTEventSource<ViewCasterUpdateEvent_MA>* ) override {
		RE::NiPointer<RE::TESObjectREFR> pRef = nullptr;
		CrosshairRefHandle = evn.value.crosshairHandler;

		if (CrosshairRefHandle != 0) {
			LookupREFRByHandle(CrosshairRefHandle, pRef);
			}


		if (pRef != nullptr) {
			if (pRef->formType == RE::ENUM_FORM_ID::kACHR) {
				RE::Actor* a = (RE::Actor*) pRef.get();
				if (a) {
					lastActor = a;
					}
				}
			}
		return RE::BSEventNotifyControl::kContinue;
		}

	static void Register() {
		static CrosshairChangeHandler_MA* pCrosshairHandler = new CrosshairChangeHandler_MA();
		uint64_t* pt = (uint64_t *) g_Source.address();

		EventSource* e = (EventSource*)*pt;

		//logger::info("g_source: pt: {:p} *pt = {:X} e: {:p} ",  (void*) pt, *pt, (void*) e);

		e->RegisterSink(pCrosshairHandler);
		CrosshairRefHandle = 0;
		CrosshairActorHandle = 0;
		}
	};


void RegisterCrosshair() {
	CrosshairChangeHandler_MA::Register();
	}

/*
* Unfortunately, returning Actor values sometimes doesn't work with Fallout4 NG (1.10.984.0)
* We instead return the X,Y,Z coordinates of the NPC and call Find.ClosestActor() in the Papyrus script
*/

std::vector<float> GetLastActorCoords(std::monostate) {
	std::vector<float> result;
	// kalli/port-240: direct gate (previously only the crosshair registration was gated, Sol review).
	if (mantella240::plugin::state().runtime_is_240 && !mantella240::plugin::allowed("GetLastActorCoords")) return result;

	if (lastActor) {
		float X = lastActor->GetPositionX();
		float Y = lastActor->GetPositionY();
		float Z = lastActor->GetPositionZ();

		//logger::info("GetLastCrosshairActor {} X:{:f} Y:{:f} Z:{:f}", lastActor->GetDisplayFullName(), X,Y, Z);
		
		result.push_back(X);
		result.push_back(Y);
		result.push_back(Z);
		}
	return result;
	}

//std::vector<RE::Actor*> GetLastCrossHairActor(std::monostate) {
//	std::vector<RE::Actor*> result;
//
//	if (lastActor) {
//		logger::info("GetLastCrosshairActor {}", lastActor->GetDisplayFullName());
//		result.push_back(lastActor);
//		}
//	logger::info("Returning from GetLastCrossHairActor");
//	return result;
//	}
